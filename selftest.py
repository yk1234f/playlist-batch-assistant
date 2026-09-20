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
from policy_selftest import PolicyRegression


def wav_bytes(seconds=90):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b'\0\0' * int(16000 * seconds))
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
        if self.path == '/short':
            body = wav_bytes(30)
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
    def test_browser_choices_only_installed_channels(self):
        from browser_runtime import launch_choices
        self.assertEqual(launch_choices(), [('Microsoft Edge', {'channel': 'msedge'}), ('Google Chrome', {'channel': 'chrome'})])
        self.assertEqual(launch_choices('chrome'), [('Google Chrome', {'channel': 'chrome'})])
        with self.assertRaises(ValueError):
            launch_choices('bundled')

    def test_missing_browsers_stop_with_install_instructions(self):
        from unittest.mock import patch, MagicMock
        from browser_provider import BrowserProvider, BrowserUnavailable
        runtime = MagicMock()
        runtime.chromium.launch_persistent_context.side_effect = RuntimeError('Executable missing')
        provider = BrowserProvider(keep_open=False)
        messages = []
        provider.report = messages.append
        with patch('playwright.sync_api.sync_playwright') as factory:
            factory.return_value.start.return_value = runtime
            with self.assertRaisesRegex(BrowserUnavailable, '请安装 Microsoft Edge'):
                provider.start()
        self.assertEqual(runtime.chromium.launch_persistent_context.call_count, 2)
        runtime.stop.assert_called_once()
        self.assertIsNone(provider.context)
        self.assertTrue(any('正在启动 Microsoft Edge' in m for m in messages))

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
        self.assertAlmostEqual(info.duration, 90)

    def test_bad_extension(self):
        with self.assertRaises(ValueError):
            validate_audio(self.audio('bad.exe'))

    def test_duration_boundary_and_short_download_cleanup(self):
        from audio_files import ShortAudioError
        path=self.root/'boundary.wav'
        path.write_bytes(wav_bytes(89.999))
        with self.assertRaises(ShortAudioError):validate_audio(path)
        path.write_bytes(wav_bytes(90))
        self.assertEqual(validate_audio(path).duration,90)
        path.write_bytes(wav_bytes(.5))
        with self.assertRaises(ShortAudioError):validate_audio(path)
        path.unlink()
        with self.assertRaises(ShortAudioError):
            download_audio(self.provider,self.candidate('/short'),self.track,self.root)
        self.assertFalse(list(self.root.iterdir()))

    def test_short_cleanup_revalidates_and_remembers_mark(self):
        from short_audio import find_short_audio,delete_short_audio,mark_deleted_short
        short=self.root/'测试 (Live) - 歌手.wav'
        full=self.root/'full.wav'
        short.write_bytes(wav_bytes(30));full.write_bytes(wav_bytes(90))
        (self.root/'bad.mp3').write_bytes(b'<html>bad</html>')
        plan,errors=find_short_audio(self.root)
        self.assertEqual(len(plan),1);self.assertTrue(errors)
        short.write_bytes(wav_bytes(90))
        deleted,errors=delete_short_audio(plan,self.root/'changed.jsonl')
        self.assertFalse(deleted);self.assertTrue(errors)
        short.write_bytes(wav_bytes(30))
        deleted,errors=delete_short_audio(plan,self.root/'short-audio-test.jsonl')
        self.assertEqual(deleted,[str(short)]);self.assertFalse(errors)
        self.assertTrue(full.exists());self.assertTrue((self.root/'bad.mp3').exists())
        mark_deleted_short([self.track],self.root)
        self.assertEqual(self.track.status,'时长不足')
        self.assertIn('已删除',self.track.note)
        alias=Track('alias','不同标签','不同歌手',note=str(short))
        mark_deleted_short([alias],self.root)
        self.assertEqual(alias.status,'时长不足')

    def test_short_candidate_metadata_skips_download(self):
        from unittest.mock import Mock
        provider=Mock()
        c=self.candidate();c.duration=89.9
        provider.search.return_value=[c]
        events=queue.Queue()
        worker=BatchWorker([self.track],[],self.root,events,provider=provider,sources=['qq'],interval=0)
        worker.process(self.track,{})
        provider.download.assert_not_called()
        self.assertTrue(any(e.get('status')=='时长不足' for e in events.queue))

    def test_local_short_is_marked_and_not_redownloaded(self):
        from unittest.mock import Mock
        (self.root/'测试 (Live) - 歌手.wav').write_bytes(wav_bytes(30))
        provider=Mock(keep_open=False)
        events=queue.Queue()
        worker=BatchWorker([self.track],[self.root],self.root,events,provider=provider,sources=['qq'],interval=0)
        worker.run()
        provider.search.assert_not_called()
        self.assertTrue(any(e.get('status')=='时长不足' for e in events.queue))

    def test_short_dialog_deletes_and_marks_task(self):
        from app import PlaylistApp
        from duplicate_dialog import DuplicateDialog
        from unittest.mock import patch
        path=self.root/'测试 (Live) - 歌手.wav'
        path.write_bytes(wav_bytes(30))
        app=PlaylistApp(state_path=self.root/'short-session.json');app.withdraw()
        app.destination.set(str(self.root));app.tracks=[self.track];app.refresh()
        dialog=DuplicateDialog(app,short=True);dialog.withdraw()
        def finish():
            end=time.monotonic()+8
            while dialog.busy and time.monotonic()<end:app.update();time.sleep(.02)
            self.assertFalse(dialog.busy)
        try:
            dialog.scan();finish()
            self.assertEqual(len(dialog.plan),1)
            with patch('duplicate_dialog.messagebox.askyesno',return_value=True):dialog.delete()
            finish()
            self.assertFalse(path.exists());self.assertEqual(self.track.status,'时长不足')
            app._load(app.state_path)
            self.assertEqual(app.tracks[0].status,'时长不足')
            self.assertIn('时长不足：1',app.stats.get())
        finally:
            with patch.object(app,'start'):dialog.close()
            for job in app.tk.splitlist(app.tk.call('after','info')):app.after_cancel(job)
            app.destroy()

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
        self.assertEqual(info.duration, 90)
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

    def test_tk_playlist_editor_crud_and_sync(self):
        from app import PlaylistApp
        from playlist_editor import PlaylistEditor
        from unittest.mock import patch
        app = PlaylistApp(state_path=self.root / 'editor-session.json')
        app.withdraw()
        editor = PlaylistEditor(app, self.root, app.playlist_saved)
        editor.withdraw()
        try:
            path = self.root / 'QQ-test.txt'
            editor.load(path)
            editor.title_value.set('初始歌曲')
            editor.artist_value.set('测试歌手')
            editor.add()
            editor.tree.selection_set('1')
            editor.title_value.set('修改歌曲 (Live)')
            editor.update()
            editor.query.set('不存在')
            self.assertEqual(len(editor.tree.get_children()), 0)
            editor.query.set('Live')
            self.assertEqual(len(editor.tree.get_children()), 1)
            with patch('playlist_editor.messagebox.showinfo'), patch.object(app, 'start') as scan:
                editor.save()
                scan.assert_called_once_with(True)
            self.assertEqual(app.tracks[0].title, '修改歌曲 (Live)')
            self.assertEqual(parse_file(path)[0].artist, '测试歌手')
            editor.tree.selection_set('1')
            editor.delete()
            with patch('playlist_editor.messagebox.showinfo'), patch.object(app, 'start'):
                editor.save()
            self.assertEqual(app.tracks, [])
            self.assertTrue(list(self.root.glob('QQ-test.txt.before-edit-*.bak')))
        finally:
            editor.destroy()
            for job in app.tk.splitlist(app.tk.call('after', 'info')):
                app.after_cancel(job)
            app.destroy()

    def test_exact_file_dedup_preserves_nonidentical_and_revalidates(self):
        from library_duplicates import find_duplicates, delete_duplicates
        original = self.root / 'original.wav'
        copy = self.root / 'original (2).wav'
        different = self.root / 'different.wav'
        original.write_bytes(wav_bytes())
        copy.write_bytes(original.read_bytes())
        different.write_bytes(wav_bytes()[:-2] + b'\x01\x01')
        plan, errors = find_duplicates(self.root)
        self.assertFalse(errors)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]['delete'], str(copy))
        copy.write_bytes(wav_bytes()[:-2] + b'\x02\x02')
        deleted, errors = delete_duplicates(plan, self.root/'changed.jsonl')
        self.assertFalse(deleted)
        self.assertTrue(errors)
        self.assertTrue(copy.exists())
        copy.write_bytes(original.read_bytes())
        deleted, errors = delete_duplicates(plan, self.root/'deleted.jsonl')
        self.assertEqual(deleted, [str(copy)])
        self.assertFalse(errors)
        self.assertTrue(original.exists())
        self.assertTrue(different.exists())

    def test_dedup_rejects_outside_root_and_fake_audio(self):
        from library_duplicates import find_duplicates, delete_duplicates
        for name in ('a.mp3','b.mp3'):
            (self.root/name).write_bytes(b'<html>not audio</html>')
        plan, errors = find_duplicates(self.root)
        self.assertFalse(plan)
        self.assertTrue(errors)
        nested = self.root/'nested'
        nested.mkdir()
        original = nested/'a.wav'
        original.write_bytes(wav_bytes())
        (nested/'b.wav').write_bytes(original.read_bytes())
        plan, _ = find_duplicates(nested)
        outside = self.root/'outside.wav'
        outside.write_bytes(original.read_bytes())
        plan[0]['delete'] = str(outside)
        deleted, errors = delete_duplicates(plan,self.root/'outside.jsonl')
        self.assertFalse(deleted)
        self.assertTrue(errors)
        self.assertTrue(outside.exists())

    def test_local_matches_are_reported_before_first_search(self):
        local = Track('local','本地歌曲','歌手')
        (self.root/'本地歌曲 - 歌手.wav').write_bytes(wav_bytes())
        events = queue.Queue()
        worker = BatchWorker([self.track,local], [self.root], self.root, events,
                             sources=['qq'],interval=0,provider=self.provider)
        worker.start();worker.join(8)
        history=list(events.queue)
        mark=next(i for i,e in enumerate(history) if e.get('uid')=='local' and e.get('status')=='本地已有')
        search=next(i for i,e in enumerate(history) if e.get('kind')=='current')
        self.assertLess(mark,search)

    def test_invalid_candidate_uses_next_exact_result_without_research(self):
        from unittest.mock import Mock
        provider=Mock()
        wrong=Candidate('其他歌曲','歌手','wrong','qq','page')
        first=Candidate(self.track.title,self.track.artist,'first','qq','page')
        second=Candidate(self.track.title,self.track.artist,'second','qq','page')
        provider.search.return_value=[first,wrong,second]
        info=Mock(duration=12,size=400000)
        provider.download.side_effect=[ValueError('文本错误'),(self.root/'ok.wav',info)]
        events=queue.Queue()
        worker=BatchWorker([self.track],[],self.root,events,sources=['qq','kugou'],retry_sources=['qq'],interval=0,provider=provider)
        worker.process(self.track,{})
        provider.search.assert_called_once_with(self.track.query,'qq')
        self.assertEqual([call.args[0] for call in provider.download.call_args_list],[first,second])
        self.assertTrue(any(e.get('status')=='成功' for e in events.queue))

    def test_sync_scans_local_without_opening_browser(self):
        from app import PlaylistApp
        from unittest.mock import patch
        path = self.root/'songs.txt'
        path.write_text('本地歌曲 - 歌手\n',encoding='utf-8')
        (self.root/'本地歌曲 - 歌手.wav').write_bytes(wav_bytes())
        app=PlaylistApp(state_path=self.root/'sync.json')
        app.withdraw()
        try:
            app.destination.set(str(self.root));app.roots=[self.root]
            app.playlist_paths=[str(path)]
            with patch('browser_provider.BrowserProvider.start') as browser:
                app.reload_playlists()
                app.worker.join(8)
                app.poll()
                self.assertEqual(app.tracks[0].status,'本地已有')
                browser.assert_not_called()
        finally:
            for job in app.tk.splitlist(app.tk.call('after','info')):app.after_cancel(job)
            app.destroy()

    def test_duplicate_dialog_preview_and_delete(self):
        from app import PlaylistApp
        from duplicate_dialog import DuplicateDialog
        from unittest.mock import patch
        a=self.root/'a.wav';b=self.root/'a (2).wav'
        a.write_bytes(wav_bytes());b.write_bytes(a.read_bytes())
        app=PlaylistApp(state_path=self.root/'duplicate-gui.json');app.withdraw()
        app.destination.set(str(self.root))
        dialog=DuplicateDialog(app);dialog.withdraw()
        def finish():
            end=time.monotonic()+8
            while dialog.busy and time.monotonic()<end:
                app.update();time.sleep(.02)
            self.assertFalse(dialog.busy)
        try:
            dialog.scan();finish()
            self.assertEqual(len(dialog.tree.get_children()),1)
            self.assertTrue(b.exists())
            with patch('duplicate_dialog.messagebox.askyesno',return_value=True):dialog.delete()
            finish()
            self.assertTrue(a.exists());self.assertFalse(b.exists())
        finally:
            with patch.object(app,'start'):dialog.close()
            for job in app.tk.splitlist(app.tk.call('after','info')):app.after_cancel(job)
            app.destroy()

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
            app.tree.selection_set(app.tracks[0].uid)
            app.track_source.set('酷狗')
            app.set_track_source()
            self.assertEqual(app.tracks[0].music_source, 'kugou')
            app.tracks[0].status = '失败'
            app.tracks[0].note = 'qq 搜索已匹配，但下载失败：Download.save_as: Target page, context or browser has been closed'
            app.autosave()
            app._load(app.state_path)
            self.assertEqual(app.tracks[0].status, '待处理')
            self.assertIn('酷狗', app.tree.item(app.tracks[0].uid, 'values')[1])
            app.browser_choice.set('Google Chrome')
            app.events.put(dict(kind='browser', name='Google Chrome'))
            app.poll()
            self.assertIn('Google Chrome', app.browser_status.get())
            app.retry_vars['qq'].set(True)
            app.by_filename.set(True)
            app.autosave()
            app._load(app.state_path)
            self.assertTrue(app.retry_vars['qq'].get())
            self.assertTrue(app.by_filename.get())
            self.assertEqual(app.browser_choice.get(), 'Google Chrome')
            self.assertEqual(app.tracks[0].music_source, 'kugou')
        finally:
            for job in app.tk.splitlist(app.tk.call('after', 'info')):
                app.after_cancel(job)
            app.destroy()


def main():
    output = Path(os.environ.get('PLAYLIST_TEST_REPORT', str(Path(tempfile.gettempdir()) / 'playlist-assistant-selftest.txt')))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8') as stream:
        suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(Regression),
                                    unittest.defaultTestLoader.loadTestsFromTestCase(PolicyRegression)])
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    if getattr(__import__('sys'), 'stdout', None):
        print(output.read_text(encoding='utf-8'))
        print(f'Report: {output}')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
