"""Browser integration tests: actual typing, DOM clicks and browser blob downloads."""
import json
import os
import queue
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from browser_provider import BrowserProvider, BrowserUnavailable
from engine import BatchWorker
from playlist_parser import Track
from selftest import wav_bytes

HTML = '''<!doctype html><html><meta charset="utf-8"><title>歌单助手浏览器集成测试</title>
<body style="font:20px sans-serif;padding:35px"><h1>网页自动操作测试</h1>
<form id="j-validator" onsubmit="return false">
<input id="j-input" placeholder="歌名 歌手">
<label><input type="radio" name="music_type" value="qq" checked>QQ</label>
<button id="j-submit" type="button">Get 搜索</button><div class="am-alert" style="display:none"></div></form>
<div id="j-player"><div class="aplayer-list"><ol></ol></div></div>
<p>当前：<input id="j-name" readonly><input id="j-author" readonly></p>
<button id="j-src-btn">下载</button><p id="progress"></p>
<script>
document.querySelector('#j-submit').onclick=()=>{
document.querySelector('#progress').textContent='已搜索：'+document.querySelector('#j-input').value;
document.querySelector('ol').innerHTML='<li><span class="aplayer-list-title">测试歌曲 (Remix)</span> - <span class="aplayer-list-author">示例歌手</span></li><li><span class="aplayer-list-title">测试歌曲 (Live)</span> - <span class="aplayer-list-author">示例歌手</span></li>';
document.querySelectorAll('li').forEach(li=>li.onclick=()=>{
document.querySelector('#j-name').value=li.querySelector('.aplayer-list-title').textContent;
document.querySelector('#j-author').value=li.querySelector('.aplayer-list-author').textContent;});
};
document.querySelector('#j-src-btn').onclick=async()=>{
const blob=await (await fetch(location.pathname==='/invalid'?'/html':location.pathname==='/flaky'?'/flaky-audio':location.pathname==='/short'?'/short-audio':'/audio')).blob();
const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='website-name.mp3';
document.body.appendChild(a);a.click();a.remove();document.querySelector('#progress').textContent='网页下载按钮已触发';};
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/short-audio':
            self.server.short_hits += 1
            body, mime = wav_bytes(30), 'audio/wav'
        elif self.path == '/flaky-audio':
            self.server.flaky_hits += 1
            body, mime = (b'<html>temporary error</html>', 'text/html') if self.server.flaky_hits == 1 else (wav_bytes(), 'audio/wav')
        elif self.path == '/audio':
            body, mime = wav_bytes(), 'audio/wav'
        elif self.path == '/html':
            body, mime = b'<html>error</html>' + b'x' * 400000, 'text/html'
        else:
            body, mime = HTML.encode(), 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        self.wfile.write(body)


class BrowserRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.provider = BrowserProvider(self.url, profile=self.root / 'profile',
                                        headless=os.getenv('PLAYLIST_BROWSER_HEADLESS') == '1',
                                        keep_open=False, timeout=10, download_timeout=10)
        self.track = Track('browser', '测试歌曲 (Live)', '示例歌手')

    def tearDown(self):
        self.provider.close()
        self.tmp.cleanup()

    def test_real_input_select_click_and_download(self):
        candidates = self.provider.search(self.track.query, 'qq')
        self.assertEqual(self.provider.page.locator('#j-input').input_value(), self.track.query)
        self.assertEqual(len(candidates), 2)
        path, info = self.provider.download(candidates[1], self.track, self.root / 'music', lambda: None)
        self.assertEqual(self.provider.page.locator('#j-name').input_value(), self.track.title)
        self.assertEqual(path.suffix, '.wav')
        self.assertEqual(path.read_bytes(), wav_bytes())
        self.assertEqual(info.duration, 90)

    def test_wrong_version_never_clicks_download(self):
        candidates = self.provider.search(self.track.query, 'qq')
        with self.assertRaisesRegex(Exception, '不匹配'):
            self.provider.download(candidates[0], self.track, self.root / 'music', lambda: None)
        self.assertFalse((self.root / 'music').exists())

    def test_blob_error_page_rejected(self):
        self.provider.base_url = self.url + 'invalid'
        candidates = self.provider.search(self.track.query, 'qq')
        with self.assertRaises(ValueError):
            self.provider.download(candidates[1], self.track, self.root / 'music', lambda: None)
        self.assertFalse(list((self.root / 'music').iterdir()))

    def test_short_download_marked_not_retried_or_saved(self):
        self.server.short_hits=0
        self.provider.base_url=self.url+'short'
        events=queue.Queue()
        worker=BatchWorker([self.track],[],self.root/'music',events,provider=self.provider,sources=['qq'],interval=0)
        worker.start();worker.join(40)
        if worker.is_alive():worker.stop();worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.server.short_hits,1)
        self.assertTrue(any(e.get('status')=='时长不足' for e in events.queue))
        self.assertFalse(list((self.root/'music').iterdir()))

    def test_stale_candidate_rejected(self):
        candidates = self.provider.search(self.track.query, 'qq')
        self.provider.search('下一首', 'qq')
        with self.assertRaisesRegex(Exception, '过期'):
            self.provider.download(candidates[1], self.track, self.root / 'music', lambda: None)

    def test_full_worker_browser_flow(self):
        events = queue.Queue()
        worker = BatchWorker([self.track], [], self.root / 'music', events,
                             sources=['qq'], interval=0, provider=self.provider)
        worker.start()
        worker.join(40)
        if worker.is_alive():
            worker.stop()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(e.get('status') == '成功' for e in events.queue), list(events.queue))
        self.assertTrue(any('点击网页下载' in e.get('text', '') for e in events.queue))

    def test_close_during_save_stops_batch_and_requeues_current(self):
        from playwright.sync_api import Download
        from unittest.mock import patch
        original = Download.save_as
        provider = self.provider
        def close_then_save(download, path):
            provider.context.close()
            return original(download, path)
        events = queue.Queue()
        worker = BatchWorker([self.track, Track('next', self.track.title, self.track.artist)], [],
                             self.root / 'music', events, sources=['qq'], interval=0, provider=provider)
        with patch.object(Download, 'save_as', close_then_save):
            worker.start()
            worker.join(40)
            if worker.is_alive():
                worker.stop()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        history = list(events.queue)
        self.assertTrue(any(e.get('uid') == 'browser' and e.get('status') == '待处理' for e in history), history)
        self.assertFalse(any(e.get('status') == '失败' for e in history), history)
        self.assertFalse(any(e.get('uid') == 'next' for e in history), history)
        self.assertTrue(any(e['kind'] == 'done' and e.get('interruption') for e in history))
        self.assertFalse(list((self.root / 'music').iterdir()))

    def test_page_closed_before_download_is_browser_interruption(self):
        candidates = self.provider.search(self.track.query, 'qq')
        self.provider.page.close()
        with self.assertRaisesRegex(BrowserUnavailable, '保留待处理'):
            self.provider.download(candidates[1], self.track, self.root / 'music', lambda: None)

    def test_default_profile_is_fresh_each_browser_run(self):
        provider = BrowserProvider(self.url, headless=True, keep_open=False)
        try:
            provider.start()
            first = provider.profile
            (first / 'old-state-marker').write_text('old')
            provider.close()
            self.assertFalse(first.exists())
            provider.start()
            self.assertNotEqual(first, provider.profile)
            self.assertFalse((provider.profile / 'old-state-marker').exists())
        finally:
            provider.close()

    def test_media_error_does_not_switch_selected_song(self):
        self.provider.search(self.track.query, 'qq')
        result = self.provider.page.evaluate('''async () => {
            const audio = document.createElement('audio');
            document.body.appendChild(audio);
            let switches = 0;
            audio.addEventListener('error', () => switches++);
            audio.addEventListener('ended', () => switches++);
            audio.preload = 'auto';
            audio.dispatchEvent(new Event('error'));
            audio.dispatchEvent(new Event('ended'));
            await audio.play();
            return {switches, paused: audio.paused, preload: audio.preload};
        }''')
        self.assertEqual(result, dict(switches=0, paused=True, preload='none'))

    def test_error_response_retry_download_without_research(self):
        self.server.flaky_hits = 0
        self.provider.base_url = self.url + 'flaky'
        candidates = self.provider.search(self.track.query, 'qq')
        generation = self.provider.generation
        path, info = self.provider.download(candidates[1], self.track, self.root/'music',lambda:None)
        self.assertEqual(self.server.flaky_hits, 2)
        self.assertEqual(self.provider.generation, generation)
        self.assertEqual(path.read_bytes(), wav_bytes())
        self.assertEqual(len(list((self.root/'music').iterdir())), 1)


def live_check(output):
    """Explicit opt-in EXE/site smoke test; never writes to the music library."""
    from matching import compare
    with tempfile.TemporaryDirectory(prefix='playlist-live-test-') as folder:
        provider = BrowserProvider(keep_open=False, timeout=35, download_timeout=90)
        def report(text):
            output.write(text + '\n')
            output.flush()
        provider.bind(lambda: None, report)
        try:
            for number, (title, artist) in enumerate([('寓言', '张韶涵'), ('越来越不懂', '蔡健雅'), ('New Boy', '房东的猫')]):
                track = Track(str(number), title, artist)
                events = queue.Queue()
                worker = BatchWorker([track], [], Path(folder), events, provider=provider, sources=['qq'], interval=0)
                provider.bind(worker.checkpoint, report)
                worker.process(track, {})
                history = list(events.queue)
                success = next((e for e in history if e.get('status') == '成功'), None)
                if not success:
                    raise RuntimeError(str([e for e in history if e.get('kind') == 'status']))
                report(f'LIVE_AUDIO_PASS {title} - {artist} bytes={success["size"]} duration={success["duration"]:.1f}')
            return 0
        except Exception as exc:
            report(f'LIVE_AUDIO_FAIL {type(exc).__name__}: {exc}')
            return 1
        finally:
            provider.close()


def main():
    path = Path(os.getenv('PLAYLIST_BROWSER_REPORT', str(Path(tempfile.gettempdir()) / 'playlist-browser-test.txt')))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as output:
        if os.getenv('PLAYLIST_LIVE_BROWSER_TEST') == '1':
            return live_check(output)
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BrowserRegression))
    import sys
    if sys.stdout:
        print(path.read_text(encoding='utf-8'))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
