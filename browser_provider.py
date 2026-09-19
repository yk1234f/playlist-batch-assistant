"""Visible page automation. Search and download originate from real UI clicks."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from audio_files import MIN_BYTES, validate_audio
from downloader import audio_extension, publish_audio
from matching import compare
from provider import BASE_URL, Candidate, ProviderError


class BrowserUnavailable(ProviderError):
    """Fatal browser/site setup failure: stop the batch, not thousands of tracks."""


class BrowserProvider:
    def __init__(self, base_url=BASE_URL, *, profile=None, headless=False,
                 timeout=30, download_timeout=90, keep_open=True, channel=None):
        self.base_url = base_url
        self.profile = profile or Path(os.getenv('LOCALAPPDATA', str(Path.home()))) / 'PlaylistBatchAssistant' / 'browser-profile'
        self.headless = headless
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.keep_open = keep_open
        self.channel = channel
        self.runtime = self.context = self.page = None
        self.checkpoint = lambda: None
        self.report = lambda message: None
        self.generation = 0
        self.rows = {}
        self.errors = []

    def bind(self, checkpoint, report):
        self.checkpoint, self.report = checkpoint, report

    def start(self):
        if self.context:
            if self.page.is_closed():
                raise BrowserUnavailable('自动浏览器已关闭；请停止后重新开始')
            return
        from playwright.sync_api import sync_playwright
        self.report('正在启动可见浏览器（Chrome / Edge）……')
        self.profile.mkdir(parents=True, exist_ok=True)
        self.runtime = sync_playwright().start()
        failures = []
        for channel in ([self.channel] if self.channel else ['chrome', 'msedge', None]):
            try:
                self.context = self.runtime.chromium.launch_persistent_context(
                    str(self.profile), channel=channel, headless=self.headless,
                    accept_downloads=True, viewport={'width': 1280, 'height': 880},
                    slow_mo=0 if self.headless else 80,
                )
                break
            except Exception as exc:
                failures.append(str(exc).splitlines()[0])
        if not self.context:
            self.close()
            raise BrowserUnavailable('无法启动自动浏览器。请安装 Chrome 或 Edge，或关闭其他新版助手后重试。' + '；'.join(failures))
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)[:300]))
        self.focus()

    def focus(self):
        if self.page and not self.page.is_closed():
            self.page.bring_to_front()

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
            if self.page.is_closed():
                raise BrowserUnavailable('自动浏览器已被关闭')
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

    def download(self, candidate, track, directory, checkpoint, min_bytes=MIN_BYTES):
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
            button.click()
            self.wait_for(lambda: bool(received), self.download_timeout, '浏览器下载')
            checkpoint()
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
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = None
            if self.runtime:
                self.runtime.stop()
                self.runtime = None
