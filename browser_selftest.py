"""Browser integration tests: actual typing, DOM clicks and browser blob downloads."""
import json
import os
import queue
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from browser_provider import BrowserProvider
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
const blob=await (await fetch(location.pathname==='/invalid'?'/html':'/audio')).blob();
const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='website-name.mp3';
document.body.appendChild(a);a.click();a.remove();document.querySelector('#progress').textContent='网页下载按钮已触发';};
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/audio':
            body, mime = wav_bytes(), 'audio/wav'
        elif self.path == '/html':
            body, mime = b'<html>error</html>' + b'x' * 400000, 'text/html'
        else:
            body, mime = HTML.encode(), 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
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
        self.assertEqual(info.duration, 12)

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


def main():
    path = Path(os.getenv('PLAYLIST_BROWSER_REPORT', str(Path(tempfile.gettempdir()) / 'playlist-browser-test.txt')))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as output:
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BrowserRegression))
    import sys
    if sys.stdout:
        print(path.read_text(encoding='utf-8'))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
