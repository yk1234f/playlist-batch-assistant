"""Lossless TXT line editing, conflict checks and atomic saves with backups."""
import hashlib
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from playlist_parser import KNOWN_FLAGS, parse_file, parse_txt_text, dedupe_tracks
from matching import identity


class PlaylistDocument:
    def __init__(self, path):
        self.path = Path(path).resolve()
        raw = self.path.read_bytes() if self.path.exists() else b''
        self.original = raw
        self.existed = self.path.exists()
        self.encoding = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
        try:
            text = raw.decode(self.encoding)
        except UnicodeDecodeError:
            self.encoding = 'gb18030'
            text = raw.decode(self.encoding)
        self.newline = '\r\n' if '\r\n' in text else '\n'
        self.lines = text.splitlines(keepends=True)
        self.dirty = False

    def tracks(self):
        return parse_txt_text(''.join(self.lines), self.path)

    def _line(self, title, artist, original=None):
        title, artist = title.strip(), artist.strip()
        if not title or not artist:
            raise ValueError('歌名、歌手不能为空')
        if any(x in title or x in artist for x in ('\r', '\n', ' - ')):
            raise ValueError('歌名/歌手不能含换行或分隔符“ - ”；多歌手请用 / 分隔')
        flags = ''.join(f'[{flag}]' for flag in KNOWN_FLAGS if original and flag in original.flags)
        text = f'{flags}{title} - {artist}'
        if original and original.album:
            text += ' - ' + original.album
        return text + self.newline

    def add(self, title, artist):
        line = self._line(title, artist)
        if self.lines and not self.lines[-1].endswith(('\n', '\r')):
            self.lines[-1] += self.newline
        self.lines.append(line)
        self.dirty = True

    def update(self, line_number, title, artist):
        original = next((t for t in self.tracks() if t.source_line == line_number), None)
        if original is None:
            raise ValueError('选中歌曲已变化，请重新选择')
        self.lines[line_number-1] = self._line(title, artist, original)
        self.dirty = True

    def delete(self, line_number):
        if not any(t.source_line == line_number for t in self.tracks()):
            raise ValueError('请选择歌曲行')
        del self.lines[line_number-1]
        self.dirty = True

    def save(self):
        current_exists = self.path.exists()
        current = self.path.read_bytes() if current_exists else b''
        if current_exists != self.existed or current != self.original:
            raise ValueError('TXT 已被其他程序修改，请重新打开后编辑；未覆盖外部修改')
        count = len(self.tracks())
        lines = [re.sub(r'^(歌曲总数\s*:\s*)\d+', lambda m: m[1]+str(count), line) for line in self.lines]
        data = ''.join(lines).encode(self.encoding)
        backup = None
        token = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        if self.existed:
            backup = self.path.with_name(self.path.name + f'.before-edit-{token}.bak')
            with backup.open('xb') as stream:
                stream.write(self.original)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.' + self.path.name + '.' + token + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.lines, self.original, self.existed, self.dirty = lines, data, True, False
        return backup


def sync_playlists(paths, previous):
    paths = list(dict.fromkeys(Path(p).resolve() for p in paths))
    known = {str(p) for p in paths}
    names = {p.name for p in paths}
    parsed = [track for path in paths for track in parse_file(path)]
    unmanaged = [t for t in previous if (t.source_path and str(Path(t.source_path).resolve()) not in known)
                 or (not t.source_path and t.source_file not in names)]
    states = {identity(t.title, t.artist): (t.status, t.note, t.music_source) for t in previous}
    unique, dupes = dedupe_tracks(unmanaged + parsed)
    for track in unique:
        old = states.get(identity(track.title, track.artist))
        if old and old[0] not in ('搜索中', '下载中'):
            track.status, track.note, track.music_source = old
    # Source line UIDs can collide with legacy session IDs; assign new view IDs.
    for track in unique:
        track.uid = uuid.uuid4().hex
    return unique, len(dupes)
