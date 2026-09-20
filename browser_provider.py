"""Visible page automation. Search and download originate from real UI clicks."""
from __future__ import annotations

import os
import time
import uuid
import tempfile
from functools import wraps
from pathlib import Path

from audio_files import MIN_BYTES, validate_audio, ShortAudioError
from downloader import audio_extension, publish_audio
from matching import compare
from provider import BASE_URL, Candidate, ProviderError
from browser_runtime import launch_choices

PREVIEW_GUARD = r'''(() => {
  // Download mode: preview playback competes for the same URL and APlayer
  // auto-advances on media errors. Keep the chosen row stable.
  const proto = HTMLMediaElement.prototype;
  const preload = Object.getOwnPropertyDescriptor(proto, 'preload');
  if (preload && preload.set) Object.defineProperty(proto, 'preload', {
    configurable: true, get: preload.get,
    set: function() { preload.set.call(this, 'none'); }
  });
  const src = Object.getOwnPropertyDescriptor(proto, 'src');
  if (src && src.set) Object.defineProperty(proto, 'src', {
    configurable: true, get: src.get,
    set: function(value) { this.preload = 'none'; src.set.call(this, value); }
  });
  proto.play = function() { this.pause(); return Promise.resolve(); };
  for (const type of ['error', 'ended']) document.addEventListener(type, event => {
    if (event.target instanceof HTMLMediaElement) {
      event.stopImmediatePropagation(); event.target.pause();
    }
  }, true);
})();'''


class BrowserUnavailable(ProviderError):
    """Fatal browser/site setup failure: stop the batch, not thousands of tracks."""


def browser_operation(method):
    """Classify browser loss at any Playwright call, including Download.save_as."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self.ensure_available()
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            if type(exc).__module__.startswith('playwright.'):
                self.ensure_available()
                if type(exc).__name__ == 'TargetClosedError':
                    raise BrowserUnavailable(self.interruption_message('浏览器连接已关闭')) from exc
            raise
    return wrapped


class BrowserProvider:
    def __init__(self, base_url=BASE_URL, *, profile=None, headless=False,
                 timeout=30, download_timeout=90, keep_open=True, channel=None, download_retries=2):
        self.base_url = base_url
        self.profile = Path(profile) if profile is not None else None
        self.temporary_profile = None
        self.headless = headless
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.keep_open = keep_open
        self.channel = channel
        self.download_retries = download_retries
        self.runtime = self.context = self.page = None
        self.checkpoint = lambda: None
        self.report = lambda message: None
        self.on_browser = lambda name: None
        self.browser_name = ''
        self.generation = 0
        self.rows = {}
        self.errors = []
        self.loss_reason = ''
        self.closing = False

    @staticmethod
    def interruption_message(reason):
        return f'{reason}，下载流程已中断；当前歌曲保留待处理，本批已停止。请保持自动浏览器开启，再点击“开始网页自动下载”重试'

    def record_loss(self, reason):
        if not self.closing:
            if not self.loss_reason:
                self.loss_reason = reason
            self.report('浏览器事件：' + reason)

    def ensure_available(self):
        reason = self.loss_reason
        if not reason and self.page and self.page.is_closed():
            reason = '自动浏览器页面已关闭'
        if reason:
            raise BrowserUnavailable(self.interruption_message(reason))

    def bind(self, checkpoint, report):
        self.checkpoint, self.report = checkpoint, report

    def start(self):
        if self.context:
            if self.page.is_closed():
                raise BrowserUnavailable('自动浏览器已关闭；请停止后重新开始')
            return
        from playwright.sync_api import sync_playwright
        self.closing = False
        self.loss_reason = ''
        self.report('正在启动已安装的可见浏览器；自动模式依次尝试 Edge、Chrome，请稍候……')
        try:
            choices = launch_choices(self.channel)
        except Exception as exc:
            raise BrowserUnavailable(str(exc)) from exc
        if self.profile is None:
            self.temporary_profile = tempfile.TemporaryDirectory(prefix='playlist-browser-', ignore_cleanup_errors=True)
            self.profile = Path(self.temporary_profile.name)
            self.report('浏览器：使用本轮独立配置，避免旧缓存或下载记录影响')
        self.profile.mkdir(parents=True, exist_ok=True)
        self.runtime = sync_playwright().start()
        failures = []
        for label, options in choices:
            try:
                self.report(f'浏览器：正在启动 {label}')
                self.context = self.runtime.chromium.launch_persistent_context(
                    str(self.profile), **options, headless=self.headless,
                    accept_downloads=True, viewport={'width': 1280, 'height': 880},
                    slow_mo=0 if self.headless else 80,
                )
                self.browser_name = label
                self.report(f'浏览器已启动：{label}')
                self.on_browser(label)
                break
            except Exception as exc:
                failures.append(f'{label}: {str(exc).splitlines()[0]}')
                self.report(f'{label} 无法启动，正在检查下一可用选项')
        if not self.context:
            self.close()
            raise BrowserUnavailable('无法启动可用浏览器；已停止下载，任务保留待处理。请安装 Microsoft Edge（https://www.microsoft.com/edge）或 Google Chrome（https://www.google.com/chrome/），安装后选择“自动”并重新开始。若已安装，请检查浏览器是否能正常打开或被系统策略拦截。本程序不会自动安装浏览器。启动详情：' + '；'.join(failures))
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.context.add_init_script(PREVIEW_GUARD)
        # Error pages may be returned with HTTP 200 and cached as a download.
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.send('Network.enable')
        self.cdp.send('Network.setCacheDisabled', {'cacheDisabled': True})
        self.page.set_default_timeout(5000)
        self.page.on('close', lambda: self.record_loss('自动浏览器页面已关闭'))
        self.page.on('crash', lambda: self.record_loss('自动浏览器页面崩溃'))
        self.context.on('close', lambda: self.record_loss('自动浏览器会话已关闭'))
        if self.context.browser:
            self.context.browser.on('disconnected', lambda: self.record_loss('自动浏览器连接已断开'))
        self.page.on('pageerror', lambda error: self.errors.append(str(error)[:300]))
        self.focus()

    @browser_operation
    def focus(self):
        if self.page and not self.page.is_closed():
            self.page.bring_to_front()

    @browser_operation
    def pump(self, milliseconds=100):
        if self.page and not self.page.is_closed():
            self.page.wait_for_timeout(milliseconds)

    def wait_for(self, predicate, seconds, description):
        deadline = time.monotonic() + seconds
        next_notice = time.monotonic() + 5
        while True:
            before_checkpoint = time.monotonic()
            self.checkpoint()
            deadline += time.monotonic() - before_checkpoint
            self.ensure_available()
            if predicate():
                return
            now = time.monotonic()
            if now >= deadline:
                detail = '；页面脚本错误：' + '；'.join(self.errors[-2:]) if self.errors else ''
                raise ProviderError(f'{description}超时（{seconds} 秒）{detail}')
            if now >= next_notice:
                self.report(f'{description}，还在等待网页响应（剩余约 {max(0, int(deadline-now))} 秒）')
                next_notice = now + 5
            self.page.wait_for_timeout(100)

    @browser_operation
    def search(self, query, source, page=1):
        self.start()
        self.checkpoint()
        self.rows.clear()
        self.generation += 1
        self.errors.clear()
        # Navigation aborts a previous fetch after skip/timeout, avoiding stale downloads.
        self.report('浏览器：打开搜索页面')
        try:
            self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=int(self.timeout*1000))
            self.page.locator('#j-input').wait_for(state='visible', timeout=int(self.timeout*1000))
        except Exception as exc:
            raise BrowserUnavailable(f'无法打开网站搜索框：{str(exc).splitlines()[0]}') from exc
        self.checkpoint()
        radio = self.page.locator(f'input[name="music_type"][value="{source}"]')
        if not radio.count():
            raise ProviderError(f'网页没有音源选项：{source}')
        self.report(f'浏览器：切换音源 {source}')
        # AmAzeUI hides native radio controls; click their visible parent label.
        radio.locator('..').click()
        if not radio.is_checked():
            raise ProviderError('音源切换未生效')
        entry = self.page.locator('#j-input')
        self.report(f'浏览器：输入「{query}」')
        entry.click()
        entry.fill('')
        entry.press_sequentially(query, delay=0 if self.headless else 45)
        self.checkpoint()
        self.report('浏览器：点击 Get 搜索按钮')
        self.page.locator('#j-submit').click()
        rows = self.page.locator('#j-player .aplayer-list li')
        alert = self.page.locator('#j-validator .am-alert').first
        try:
            self.wait_for(lambda: rows.count() > 0 or (alert.count() and alert.is_visible() and alert.inner_text().strip()),
                          self.timeout, '搜索结果')
        except ProviderError as exc:
            if self.errors:
                raise BrowserUnavailable(str(exc)) from exc
            raise
        if not rows.count():
            text = alert.inner_text().strip()[:300]
            self.report('网页提示：' + text)
            if any(word in text for word in ('没有找到', '未找到', '没有相关', '无结果')):
                return []
            raise ProviderError('网页提示：' + text)
        candidates = []
        for i in range(min(rows.count(), 50)):
            row = rows.nth(i)
            title = row.locator('.aplayer-list-title').inner_text().strip()
            artist = row.locator('.aplayer-list-author').inner_text().strip()
            if not title or not artist:
                continue
            candidate = Candidate(title, artist, '', source, self.page.url)
            self.rows[id(candidate)] = (self.generation, i)
            candidates.append(candidate)
        self.report(f'浏览器：网页显示 {len(candidates)} 条候选，正在匹配版本')
        return candidates

    @browser_operation
    def download(self, candidate, track, directory, checkpoint, min_bytes=MIN_BYTES):
        for attempt in range(self.download_retries + 1):
            if attempt:
                self.report(f'下载返回无效内容，{attempt * 3} 秒后重试当前歌曲下载（{attempt}/{self.download_retries}）；不重新搜索音源')
                deadline = time.monotonic() + attempt * 3
                while time.monotonic() < deadline:
                    checkpoint()
                    self.ensure_available()
                    self.pump(100)
            try:
                return self._download_once(candidate, track, directory, checkpoint, min_bytes)
            except ShortAudioError:
                raise
            except ValueError as exc:
                if attempt == self.download_retries:
                    raise ValueError(f'{exc}；当前下载已重试 {self.download_retries} 次') from exc

    def _download_once(self, candidate, track, directory, checkpoint, min_bytes):
        position = self.rows.get(id(candidate))
        if not position or position[0] != self.generation:
            raise ProviderError('候选页面已过期，必须重新搜索')
        checkpoint()
        self.report(f'浏览器：点击候选「{candidate.title} - {candidate.artist}」')
        row = self.page.locator('#j-player .aplayer-list li').nth(position[1])
        row.scroll_into_view_if_needed()
        row.click()
        selected_title = self.page.locator('#j-name')
        selected_artist = self.page.locator('#j-author')
        self.wait_for(lambda: selected_title.input_value().strip() == candidate.title
                      and selected_artist.input_value().strip() == candidate.artist, 8, '候选切换')
        if not compare(track.title, track.artist, selected_title.input_value(), selected_artist.input_value()).automatic:
            raise ProviderError('网页实际选中歌曲不匹配，已停止点击下载')
        checkpoint()
        received = []
        handler = lambda download: received.append(download)
        self.page.on('download', handler)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f'.{uuid.uuid4().hex}.part'
        succeeded = False
        try:
            self.report('浏览器：点击网页下载按钮，等待浏览器生成文件')
            button = self.page.locator('#j-src-btn')
            button.scroll_into_view_if_needed()
            if (selected_title.input_value().strip(), selected_artist.input_value().strip()) != (candidate.title, candidate.artist):
                raise ProviderError('网页自动切换了歌曲，已阻止错歌下载；请重新开始')
            button.click()
            self.wait_for(lambda: bool(received), self.download_timeout, '浏览器下载')
            checkpoint()
            self.report('浏览器：正在保存下载文件，请保持自动浏览器开启')
            received[0].save_as(str(temp))
            if received[0].failure():
                raise ProviderError('浏览器下载失败：' + str(received[0].failure()))
            if temp.stat().st_size > 512 * 1024 * 1024:
                raise ValueError('文件超过最大大小 512 MiB')
            with temp.open('rb') as file:
                extension = audio_extension(file.read(4096), '', '')
            self.report('浏览器下载结束，正在校验真实音频格式、大小和时长')
            info = validate_audio(temp, min_bytes, extension)
            checkpoint()
            destination = publish_audio(temp, directory, track, extension)
            succeeded = True
            self.report(f'校验通过：{destination.name}，{info.size // 1024} KiB / {info.duration:.1f} 秒')
            return destination, info
        finally:
            self.page.remove_listener('download', handler)
            for download in received:
                try:
                    download.delete()
                except Exception:
                    pass
            temp.unlink(missing_ok=True)
            if not succeeded and not received and not self.page.is_closed():
                # Abort pending blob fetch before another track can be processed.
                try:
                    self.page.goto('about:blank', timeout=5000)
                except Exception:
                    pass

    def close(self):
        self.closing = True
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = None
            self.page = None
            if self.runtime:
                self.runtime.stop()
                self.runtime = None
            if self.temporary_profile:
                self.temporary_profile.cleanup()
                self.temporary_profile = None
                self.profile = None
