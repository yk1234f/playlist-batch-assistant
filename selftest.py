"""Offline regression suite. Generates its own silent audio; downloads no music."""
from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from audio_files import validate_audio, scan_library
from downloader import download_audio
from engine import BatchWorker
from matching import compare, identity, safe_stem
from playlist_parser import Track, parse_file, dedupe_tracks
from provider import JBSou, Candidate, ProviderError


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b'\0\0' * 16000 * 12)
    return stream.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        form = parse_qs(data.decode())
        self.server.forms.append(form)
        query = form.get('input', [''])[0]
        if query.startswith('missing'):
            result = {'code': 404, 'data': '', 'error': '没有找到相关信息'}
        else:
            result = {'code': 200, 'data': [dict(name='测试 (Live)', artist='歌手', url='/audio')]}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = self.server.audio
        content_type = 'audio/wav'
        declared = len(body)
        if self.path == '/html':
            body = b'<html>Error</html>' + b' ' * 400000
            content_type = 'audio/mpeg'
            declared = len(body)
        elif self.path == '/wrongmime':
            content_type = 'text/html'
        elif self.path == '/truncated':
            body = body[:300000]
        elif self.path == '/tiny':
            body = body[:100]
            declared = len(body)
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(declared))
        self.end_headers()
        self.wfile.write(body)


class Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.audio = wav_bytes()
        cls.server.forms = []
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
        self.provider = JBSou(self.url, timeout=2)
        self.track = Track('one', '测试 (Live)', '歌手')

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, path='audio'):
        return Candidate(self.track.title, self.track.artist, self.url + path, 'qq', self.url)

    def audio(self, name):
        path = self.root / name
        path.write_bytes(self.server.audio)
        return path

    def test_artist_order_and_unicode(self):
        self.assertEqual(identity('歌（Live）', 'A / B'), identity('歌 (live)', 'b&a'))

    def test_live_chinese_suffix(self):
        self.assertEqual(identity('歌 (Live版)', 'A'), identity('歌 (Live)', 'A'))
        self.assertFalse(compare('歌 (Live版)', 'A', '歌', 'A').automatic)

    def test_repair_garbled_tag(self):
        from audio_files import repair_tag
        self.assertEqual(repair_tag('给我一个理由忘记'.encode('gbk').decode('latin-1')), '给我一个理由忘记')
        self.assertEqual(repair_tag('A-Lin'), 'A-Lin')

    def test_hyphenated_artist_filename(self):
        from audio_files import file_identity
        self.assertIn(('给我一个理由忘记', 'A-Lin'), file_identity(self.audio('A-Lin-给我一个理由忘记.wav')))

    def test_live_studio_distinct(self):
        self.assertFalse(compare('歌 (Live)', 'A', '歌', 'A').automatic)

    def test_remix_different_versions(self):
        self.assertNotEqual(identity('歌 (DJ One Remix)', 'A'), identity('歌 (DJ Two Remix)', 'A'))
        self.assertFalse(compare('歌 (Remix)', 'A', '歌 (Live)', 'A').automatic)

    def test_featured_artist_not_dropped(self):
        self.assertFalse(compare('歌', 'A / B', '歌', 'A').automatic)

    def test_speed_preserved(self):
        self.assertFalse(compare('歌 (0.8x)', 'A', '歌', 'A').automatic)

    def test_similar_title_review_only(self):
        self.assertFalse(compare('Something Special', 'A', 'Something Specials', 'A').automatic)

    def test_safe_filename(self):
        self.assertNotIn('/', safe_stem('../测试:*', 'A/B'))
        self.assertLessEqual(len(safe_stem('a' * 200, 'b')), 160)

    def test_playlist_dedupe(self):
        unique, dupes = dedupe_tracks([self.track, Track('two', '测试（live）', '歌手'), Track('three', '测试', '歌手')])
        self.assertEqual((len(unique), len(dupes)), (2, 1))

    def test_txt_parse(self):
        path = self.root / 'test.txt'
        path.write_text('歌单名称: test\n测试 (Live) - 歌手 - 专辑\n', encoding='utf-8-sig')
        self.assertEqual(parse_file(path)[0].album, '专辑')

    def test_uid_different_folders(self):
        first = self.root / 'test.txt'
        first.write_text('歌 - A', encoding='utf-8')
        folder = self.root / 'other'
        folder.mkdir()
        second = folder / first.name
        second.write_text('歌 - B', encoding='utf-8')
        self.assertNotEqual(parse_file(first)[0].uid, parse_file(second)[0].uid)

    def test_valid_audio(self):
        info = validate_audio(self.audio('valid.wav'))
        self.assertAlmostEqual(info.duration, 12)

    def test_bad_extension(self):
        with self.assertRaises(ValueError):
            validate_audio(self.audio('bad.exe'))

    def test_fake_html_mp3(self):
        path = self.root / 'bad.mp3'
        path.write_bytes(b'<html>' + b'x' * 400000)
        with self.assertRaises(ValueError):
            validate_audio(path)

    def test_fake_id3(self):
        path = self.root / 'bad.mp3'
        path.write_bytes(b'ID3' + b'\0' * 400000)
        with self.assertRaises(Exception):
            validate_audio(path)

    def test_truncated_wav(self):
        path = self.root / 'bad.wav'
        path.write_bytes(self.server.audio[:300000])
        with self.assertRaises(ValueError):
            validate_audio(path)

    def test_scan_filename_orders_and_overlap(self):
        path = self.audio('歌手 - 测试 (Live).wav')
        index, errors = scan_library([self.root, self.root])
        self.assertEqual(index[identity(self.track.title, self.track.artist)], path)
        self.assertEqual(errors, [])
        self.assertNotIn(identity('测试', '歌手'), index)

    def test_scan_does_not_dedupe_invalid_file(self):
        path = self.root / '测试 (Live) - 歌手.mp3'
        path.write_bytes(b'<html>' + b'x' * 400000)
        index, errors = scan_library([self.root])
        self.assertFalse(index)
        self.assertEqual(len(errors), 1)

    def test_real_http_provider_contract(self):
        result = self.provider.search('测试 歌手', 'qq')
        self.assertEqual(result[0].url, self.url + 'audio')
        self.assertEqual(self.server.forms[-1]['filter'], ['name'])
        self.assertEqual(self.server.forms[-1]['type'], ['qq'])

    def test_no_results(self):
        self.assertEqual(self.provider.search('missing', 'qq'), [])

    def test_download_and_validate(self):
        path, info = download_audio(self.provider, self.candidate(), self.track, self.root)
        self.assertEqual(path.suffix, '.wav')
        self.assertEqual(path.read_bytes(), self.server.audio)
        self.assertEqual(info.duration, 12)
        self.assertFalse(list(self.root.glob('*.part')))

    def test_no_overwrite(self):
        first, _ = download_audio(self.provider, self.candidate(), self.track, self.root)
        second, _ = download_audio(self.provider, self.candidate(), self.track, self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), self.server.audio)

    def test_download_failures_leave_no_files(self):
        for route in ('html', 'wrongmime', 'truncated', 'tiny'):
            with self.subTest(route=route):
                with self.assertRaises(Exception):
                    download_audio(self.provider, self.candidate(route), self.track, self.root)
                self.assertFalse(list(self.root.iterdir()))

    def test_cancel_cleans_partial(self):
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise InterruptedError()
        with self.assertRaises(InterruptedError):
            download_audio(self.provider, self.candidate(), self.track, self.root, cancel)
        self.assertFalse(list(self.root.iterdir()))

    def run_worker(self, tracks=None, **kw):
        events = queue.Queue()
        worker = BatchWorker(tracks or [self.track], [self.root], self.root, events,
                             provider=self.provider, sources=['qq'], interval=0, **kw)
        worker.start()
        worker.join(8)
        self.assertFalse(worker.is_alive())
        return list(events.queue)

    def test_queue_end_to_end_and_second_run_dedupe(self):
        events = self.run_worker()
        self.assertTrue(any(e.get('status') == '成功' for e in events))
        events = self.run_worker()
        self.assertTrue(any(e.get('status') == '本地已有' for e in events))
        self.assertFalse(any(e.get('status') == '下载中' for e in events))

    def test_missing_success_requeues(self):
        self.track.status = '成功'
        events = self.run_worker()
        self.assertTrue(any(e.get('status') == '待处理' for e in events))
        self.assertTrue(any(e.get('status') == '成功' for e in events))

    def test_mismatch_never_downloads(self):
        self.track.title = '测试 (Remix)'
        events = self.run_worker()
        self.assertTrue(any(e.get('status') == '待确认' for e in events))
        self.assertFalse(list(self.root.iterdir()))

    def test_scan_only_no_network(self):
        count = len(self.server.forms)
        self.run_worker(scan_only=True)
        self.assertEqual(len(self.server.forms), count)

    def test_pause_resume(self):
        events = queue.Queue()
        worker = BatchWorker([self.track], [self.root], self.root, events, provider=self.provider, sources=['qq'], interval=0)
        worker.pause()
        worker.start()
        time.sleep(.15)
        self.assertFalse(list(self.root.iterdir()))
        self.assertTrue(worker.is_alive())
        worker.resume()
        worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(e.get('status') == '成功' for e in events.queue))

    def test_stop_while_paused(self):
        events = queue.Queue()
        worker = BatchWorker([self.track], [self.root], self.root, events, provider=self.provider)
        worker.pause()
        worker.start()
        worker.stop()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(list(self.root.iterdir()))

    def test_skip_current_then_next(self):
        events = queue.Queue()
        original = self.provider.search
        worker = BatchWorker([self.track, Track('two', '测试 (Live)', '歌手')], [self.root], self.root,
                             events, provider=self.provider, sources=['qq'], interval=0)
        first = True
        def search(*args):
            nonlocal first
            if first:
                first = False
                worker.skip()
            return original(*args)
        self.provider.search = search
        worker.start()
        worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(e.get('uid') == 'one' and e.get('status') == '跳过' for e in events.queue))
        self.assertTrue(any(e.get('uid') == 'two' and e.get('status') == '成功' for e in events.queue))

    def test_tk_session_and_controls(self):
        from app import PlaylistApp
        app = PlaylistApp(state_path=self.root / 'session.json')
        app.withdraw()
        try:
            app.tracks = [self.track]
            app.duplicates = 2
            app.destination.set(str(self.root))
            app.roots = [self.root]
            app.refresh()
            app.update_idletasks()
            app.autosave()
            app.tracks = []
            app._load(app.state_path)
            self.assertEqual(len(app.tracks), 1)
            self.assertEqual(app.duplicates, 2)
            self.assertEqual(len(app.tree.get_children()), 1)
            self.assertIn('歌单歌曲：3', app.stats.get())
            app.events.put(dict(kind='current', uid=app.tracks[0].uid))
            app.events.put(dict(kind='match', uid=app.tracks[0].uid, page=self.url,
                                title=1., artist=1., version=True, candidate='测试 (Live) - 歌手'))
            app.poll()
            self.assertIn('100%', app.match.get())
            self.assertEqual(app.current_page, self.url)
        finally:
            for job in app.tk.splitlist(app.tk.call('after', 'info')):
                app.after_cancel(job)
            app.destroy()


def main():
    output = Path(os.environ.get('PLAYLIST_TEST_REPORT', str(Path(tempfile.gettempdir()) / 'playlist-assistant-selftest.txt')))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Regression))
    if getattr(__import__('sys'), 'stdout', None):
        print(output.read_text(encoding='utf-8'))
        print(f'Report: {output}')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
