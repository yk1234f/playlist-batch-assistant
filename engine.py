from __future__ import annotations

import threading
import time
from pathlib import Path

from audio_files import MIN_BYTES, scan_library, validate_audio
from matching import compare, identity
from downloader import download_audio
from provider import JBSou, SOURCES
from source_policy import search_plan
from browser_provider import BrowserProvider, BrowserUnavailable


class Cancelled(Exception):
    pass


class Skipped(Exception):
    pass


class BatchWorker(threading.Thread):
    def __init__(self, tracks, roots, destination, events, *, sources=None,
                 min_bytes=MIN_BYTES, interval=2.0, provider=None, scan_only=False,
                 retry_sources=(), sources_by_filename=False):
        super().__init__(daemon=True)
        self.tracks = tracks
        self.roots = list(dict.fromkeys([*roots, destination]))
        self.destination = destination
        self.events = events
        self.sources = list(SOURCES.values()) if sources is None else list(sources)
        self.retry_sources = list(retry_sources)
        self.sources_by_filename = sources_by_filename
        self.min_bytes = min_bytes
        self.interval = interval
        self.provider = provider or BrowserProvider()
        self.scan_only = scan_only
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.cancel_event = threading.Event()
        self.skip_event = threading.Event()
        self.last_request = 0.0
        self.active_uid = None
        self.control_lock = threading.Lock()
        self.focus_event = threading.Event()
        self.current_step = ''
        if hasattr(self.provider, 'bind'):
            self.provider.bind(self.checkpoint, self.report)

    def report(self, message):
        self.current_step = message
        self.emit('message', text=message)

    def focus_browser(self):
        self.focus_event.set()

    def service_focus(self):
        if self.focus_event.is_set():
            self.focus_event.clear()
            if hasattr(self.provider, 'focus'):
                self.provider.focus()

    def emit(self, kind, **data):
        self.events.put(dict(kind=kind, **data))

    def pause(self):
        self.resume_event.clear()

    def resume(self):
        self.resume_event.set()

    def stop(self):
        self.cancel_event.set()
        self.resume_event.set()

    def skip(self):
        with self.control_lock:
            if self.active_uid is not None:
                self.skip_event.set()

    def checkpoint(self):
        while True:
            self.service_focus()
            if self.cancel_event.is_set():
                raise Cancelled()
            if self.skip_event.is_set():
                raise Skipped()
            if self.resume_event.wait(.1):
                return
            if hasattr(self.provider, 'pump'):
                self.provider.pump()

    def pace(self):
        while time.monotonic() - self.last_request < self.interval:
            self.checkpoint()
            time.sleep(.05)
        self.checkpoint()
        self.last_request = time.monotonic()

    def run(self):
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
            self.emit('message', text='正在扫描本地音乐，读取标签并检查文件……')
            index, warnings = scan_library(self.roots, self.min_bytes, self.checkpoint)
            self.emit('scan', warnings=warnings, count=len(set(index.values())))
            for track in self.tracks:
                self.checkpoint()
                key = identity(track.title, track.artist)
                local = index.get(key)
                if local:
                    try:
                        validate_audio(local, self.min_bytes)
                    except Exception:
                        index.pop(key, None)
                        local = None
                if local:
                    self.emit('status', uid=track.uid, status='本地已有', note=str(local))
                    continue
                if track.status in ('成功', '本地已有'):
                    self.emit('status', uid=track.uid, status='待处理', note='本地文件缺失或校验失败，重新排队')
                if self.scan_only or track.status in ('跳过', '待确认', '失败'):
                    continue
                with self.control_lock:
                    self.skip_event.clear()
                    self.active_uid = track.uid
                self.emit('current', uid=track.uid)
                try:
                    self.process(track, index)
                except Skipped:
                    self.emit('status', uid=track.uid, status='跳过', note='用户跳过')
                except Cancelled:
                    self.emit('status', uid=track.uid, status='待处理', note='已停止，可继续')
                    raise
                except BrowserUnavailable as exc:
                    self.emit('status', uid=track.uid, status='待处理', note=str(exc))
                    raise
                except Exception as exc:
                    self.emit('status', uid=track.uid, status='失败', note=str(exc))
                finally:
                    with self.control_lock:
                        self.active_uid = None
                        self.skip_event.clear()
        except Cancelled:
            self.emit('message', text='已停止，未完成歌曲可以继续')
        except Exception as exc:
            self.emit('message', text=f'任务错误：{exc}')
        finally:
            self.emit('done')
            try:
                if getattr(self.provider, 'keep_open', False) and self.provider.page:
                    while not self.cancel_event.wait(.1) and not self.provider.page.is_closed():
                        self.service_focus()
                        self.provider.pump()
            finally:
                if hasattr(self.provider, 'close'):
                    self.provider.close()

    def process(self, track, index):
        self.emit('status', uid=track.uid, status='搜索中', note='')
        best = None
        errors = []
        plan = search_plan(track, self.sources, self.retry_sources, self.sources_by_filename)
        if not plan:
            self.emit('status', uid=track.uid, status='待确认', note='没有可用音源，请勾选音源或修改歌单文件名')
            return
        for source, query, round_number in plan:
            self.pace()
            self.emit('source', uid=track.uid, source=source, round=round_number)
            self.report(f'音源 {source} 第 {round_number} 次：搜索「{query}」')
            try:
                candidates = self.provider.search(query, source)
            except (Cancelled, Skipped, BrowserUnavailable):
                raise
            except Exception as exc:
                errors.append(f'{source}: {exc}')
                self.report(f'音源 {source} 搜索失败：{exc}')
                continue
            self.checkpoint()
            if not candidates:
                self.report(f'音源 {source} 没有候选，按已勾选策略继续')
            ranked = sorted([(compare(track.title, track.artist, c.title, c.artist), c)
                             for c in candidates], key=lambda item: item[0].score, reverse=True)
            for match, candidate in ranked:
                if best is None or match.score > best[0].score:
                    best = match, candidate
                    self.emit('match', uid=track.uid, title=match.title, artist=match.artist,
                              version=match.version, candidate=f'{candidate.title} - {candidate.artist}',
                              page=candidate.page_url)
                if not match.automatic:
                    continue
                self.report(f'已找到精确匹配，停止其他音源和二次搜索：{source}')
                self.emit('status', uid=track.uid, status='下载中', note=f'{source}: {candidate.title}')
                self.pace()
                try:
                    if hasattr(self.provider, 'download'):
                        path, info = self.provider.download(candidate, track, self.destination,
                                                           self.checkpoint, self.min_bytes)
                    else:
                        path, info = download_audio(self.provider, candidate, track, self.destination,
                                                    self.checkpoint, self.min_bytes)
                except (Cancelled, Skipped, BrowserUnavailable):
                    raise
                except Exception as exc:
                    self.emit('status', uid=track.uid, status='失败',
                              note=f'{source} 搜索已匹配，但下载失败：{exc}；本首停止搜索，可更换音源后重试')
                    return
                index[identity(track.title, track.artist)] = path
                self.emit('status', uid=track.uid, status='成功', note=str(path),
                          duration=info.duration, size=info.size)
                return
        status = '待确认' if best else '失败'
        note = '候选歌名/歌手/版本不完全一致，请核对' if best else '所选音源未找到匹配歌曲'
        if errors:
            note += '；' + '；'.join(errors)[-1500:]
        self.emit('status', uid=track.uid, status=status, note=note)
