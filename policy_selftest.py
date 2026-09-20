import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from engine import BatchWorker
from playlist_parser import Track, parse_file
from playlist_document import PlaylistDocument, sync_playlists
from source_policy import filename_sources, selected_for, search_plan
from provider import Candidate


class SpyProvider:
    def __init__(self, finder, fail_download=False):
        self.finder = finder
        self.calls = []
        self.downloads = []
        self.fail_download = fail_download

    def search(self, query, source):
        self.calls.append((source, query))
        return self.finder(query, source)

    def download(self, candidate, track, directory, checkpoint, min_bytes):
        self.downloads.append(candidate)
        if self.fail_download:
            raise ValueError('网站获取失败')
        return directory / 'test.wav', SimpleNamespace(duration=12, size=400000)


class PolicyRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.track = Track('test', '歌曲 (Live)', '歌手', source_file='歌单.txt')

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self):
        return Candidate(self.track.title, self.track.artist, 'http://localhost/audio', 'qq', 'http://localhost/')

    def process(self, provider, **kwargs):
        events = queue.Queue()
        worker = BatchWorker([self.track], [], self.root, events, provider=provider, interval=0, **kwargs)
        worker.process(self.track, {})
        return list(events.queue)

    def test_default_once_per_source(self):
        provider = SpyProvider(lambda q, s: [])
        self.process(provider, sources=['qq', 'kuwo'])
        self.assertEqual(provider.calls, [('qq', self.track.query), ('kuwo', self.track.query)])

    def test_second_round_only_checked_sources(self):
        provider = SpyProvider(lambda q, s: [])
        self.process(provider, sources=['qq', 'kuwo'], retry_sources=['qq'])
        self.assertEqual(provider.calls, [('qq', self.track.query), ('kuwo', self.track.query), ('qq', self.track.title)])

    def test_match_stops_all_searches(self):
        provider = SpyProvider(lambda q, s: [self.candidate()])
        self.process(provider, sources=['qq', 'kuwo'], retry_sources=['qq', 'kuwo'])
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(provider.downloads), 1)

    def test_download_error_does_not_restart_search(self):
        provider = SpyProvider(lambda q, s: [self.candidate()], fail_download=True)
        events = self.process(provider, sources=['qq', 'kuwo'], retry_sources=['qq', 'kuwo'])
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(any(e.get('status') == '失败' for e in events))

    def test_search_error_can_retry_if_checked(self):
        def find(query, source):
            if query == self.track.query:
                raise TimeoutError('timeout')
            return [self.candidate()]
        provider = SpyProvider(find)
        events = self.process(provider, sources=['qq'], retry_sources=['qq'])
        self.assertEqual(provider.calls, [('qq', self.track.query), ('qq', self.track.title)])
        self.assertTrue(any(e.get('status') == '成功' for e in events))

    def test_wrong_version_does_not_stop_search(self):
        def find(query, source):
            return [Candidate('歌曲', '歌手', '', source, '')] if source == 'qq' else [self.candidate()]
        provider = SpyProvider(find)
        self.process(provider, sources=['qq', 'kuwo'])
        self.assertEqual(len(provider.calls), 2)

    def test_empty_source_selection_stays_empty(self):
        provider = SpyProvider(lambda q, s: [self.candidate()])
        self.process(provider, sources=[])
        self.assertEqual(provider.calls, [])

    def test_filename_source_override_and_fallback(self):
        self.track.source_file = 'QQ音乐_喜欢.txt'
        self.assertEqual(selected_for(self.track, ['netease'], True), ['qq'])
        self.track.source_file = '喜欢.txt'
        self.assertEqual(selected_for(self.track, ['netease'], True), ['netease'])
        self.assertEqual(filename_sources('网易_酷狗_ＱＱ.txt'), ['netease', 'qq', 'kugou'])

    def test_per_track_override_priority(self):
        self.track.source_file = '网易.txt'
        self.track.music_source = 'migu'
        self.assertEqual(selected_for(self.track, ['qq'], True), ['migu'])

    def test_undeclared_retry_source_never_searched(self):
        self.assertEqual(search_plan(self.track, ['qq'], ['kuwo']), [('qq', self.track.query, 1)])

    def test_txt_add_update_delete_preserves_header_and_backup(self):
        path = self.root / '网易歌单.txt'
        original = '歌单名称: demo\r\n歌曲总数: 2\r\n[VIP专享]旧歌 - A - 专辑\r\n删除我 - B - 专辑2\r\n'.encode('utf-8-sig')
        path.write_bytes(original)
        doc = PlaylistDocument(path)
        doc.update(3, '新歌', '新歌手')
        doc.delete(4)
        doc.add('另一首 (Live)', 'C / D')
        backup = doc.save()
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn(b'\r\n', path.read_bytes())
        tracks = parse_file(path)
        self.assertEqual([(t.title, t.artist) for t in tracks], [('新歌', '新歌手'), ('另一首 (Live)', 'C / D')])
        self.assertEqual(tracks[0].album, '专辑')
        self.assertIn('VIP专享', tracks[0].flags)

    def test_txt_gb_encoding_and_external_conflict(self):
        path = self.root / 'gb.txt'
        path.write_bytes('旧歌 - 歌手\r\n'.encode('gb18030'))
        doc = PlaylistDocument(path)
        doc.update(1, '新歌', '歌手')
        doc.save()
        self.assertEqual(path.read_bytes().decode('gb18030'), '新歌 - 歌手\r\n')
        doc.add('另一首', 'A')
        path.write_text('外部修改 - B', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '其他程序'):
            doc.save()
        self.assertEqual(path.read_text(encoding='utf-8'), '外部修改 - B')

    def test_txt_empty_new_and_invalid_fields(self):
        path = self.root / 'new.txt'
        doc = PlaylistDocument(path)
        with self.assertRaises(ValueError):
            doc.add('', 'A')
        with self.assertRaises(ValueError):
            doc.add('bad\nline', 'A')
        doc.add('歌', 'A')
        self.assertIsNone(doc.save())
        self.assertEqual(len(parse_file(path)), 1)

    def test_task_sync_preserves_unchanged_and_resets_edited(self):
        path = self.root / 'QQ.txt'
        path.write_text('旧歌 - A\n保留 - B\n删除 - C\n', encoding='utf-8')
        previous = parse_file(path)
        for t in previous:
            t.status, t.music_source = '成功', 'kuwo'
        doc = PlaylistDocument(path)
        doc.update(1, '新歌', 'A')
        doc.delete(3)
        doc.add('新增', 'D')
        doc.save()
        tasks, dupes = sync_playlists([path], previous)
        self.assertEqual([t.title for t in tasks], ['新歌', '保留', '新增'])
        self.assertEqual([t.status for t in tasks], ['待处理', '成功', '待处理'])
        self.assertEqual(tasks[1].music_source, 'kuwo')

    def test_task_sync_keeps_other_playlist_duplicate(self):
        a, b = self.root / 'a.txt', self.root / 'b.txt'
        a.write_text('歌 - A\n', encoding='utf-8')
        b.write_text('歌 - A\n', encoding='utf-8')
        previous, dupes = sync_playlists([a, b], [])
        self.assertEqual(dupes, 1)
        a.write_text('', encoding='utf-8')
        tasks, dupes = sync_playlists([a, b], previous)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(dupes, 0)

    def test_source_event_exposes_active_source(self):
        provider = SpyProvider(lambda q, s: [self.candidate()])
        events = self.process(provider, sources=['migu'])
        self.assertTrue(any(e.get('kind') == 'source' and e['source'] == 'migu' for e in events))
