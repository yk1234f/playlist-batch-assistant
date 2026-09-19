from __future__ import annotations

import os
import struct
from pathlib import Path
from dataclasses import dataclass
import mutagen

EXTENSIONS = {'.mp3', '.flac', '.wav', '.ogg', '.opus', '.m4a', '.aac', '.ape', '.wma', '.aiff', '.aif'}
MIN_BYTES = 256 * 1024
MIN_SECONDS = 10.0


@dataclass
class AudioInfo:
    extension: str
    duration: float
    size: int


def validate_audio(path: Path, min_bytes: int = MIN_BYTES, extension: str | None = None,
                   content_type: str = '', min_seconds: float = MIN_SECONDS) -> AudioInfo:
    suffix = (extension or path.suffix).lower()
    if suffix not in EXTENSIONS:
        raise ValueError('不是支持的音频扩展名')
    size = path.stat().st_size
    if size < min_bytes:
        raise ValueError(f'文件太小：{size} 字节，要求至少 {min_bytes} 字节')
    mime = content_type.split(';')[0].strip().lower()
    if mime.startswith('text/') or any(x in mime for x in ('json', 'html', 'xml')):
        raise ValueError('服务器返回文本/HTML/JSON，而非音频')
    with path.open('rb') as f:
        header = f.read(4096)
    stripped = header.lstrip(b'\xef\xbb\xbf \r\n\t').lower()
    if stripped.startswith((b'<', b'{', b'[')):
        raise ValueError('文件内容是网页或错误消息')
    signatures = {
        '.flac': header.startswith(b'fLaC'),
        '.wav': header.startswith(b'RIFF') and header[8:12] == b'WAVE',
        '.ogg': header.startswith(b'OggS'), '.opus': header.startswith(b'OggS'),
        '.m4a': header[4:8] == b'ftyp', '.ape': header.startswith(b'MAC '),
        '.wma': header.startswith(bytes.fromhex('3026b2758e66cf11')),
        '.aiff': header.startswith(b'FORM'), '.aif': header.startswith(b'FORM'),
        '.mp3': header.startswith(b'ID3') or (len(header) > 1 and header[0] == 255 and header[1] & 224 == 224),
        '.aac': header.startswith(b'ADIF') or (len(header) > 1 and header[0] == 255 and header[1] & 246 == 240),
    }
    if not signatures.get(suffix):
        raise ValueError('音频文件头与扩展名不符')
    audio = mutagen.File(path)
    duration = float(getattr(getattr(audio, 'info', None), 'length', 0))
    if audio is None or duration < min_seconds:
        raise ValueError('无法解析音频或时长不足（可能为截断文件/试听片段）')
    if suffix == '.wav':
        expected = struct.unpack('<I', header[4:8])[0] + 8
        if size < expected:
            raise ValueError('WAV 文件被截断')
    return AudioInfo(suffix, duration, size)


def detect_directories() -> list[Path]:
    """Known folders only; do not crawl all disks."""
    home = Path.home()
    paths = [home / 'Music', home / 'Downloads', home / 'Downloads' / 'PlaylistAssistant',
             home / 'Music' / 'PlaylistAssistant']
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                for name in ('My Music', '{374DE290-123F-4565-9164-39C4925E467B}'):
                    try:
                        paths.append(Path(os.path.expandvars(winreg.QueryValueEx(key, name)[0])))
                    except OSError:
                        pass
        except OSError:
            pass
    return list(dict.fromkeys(p.resolve() for p in paths if p.is_dir()))


def file_identity(path: Path) -> list[tuple[str, str]]:
    from matching import versions
    result = []
    tagged = None
    try:
        audio = mutagen.File(path, easy=True)
        if audio is not None:
            title = audio.get('title', [])
            artist = audio.get('artist', [])
            if title and artist:
                tagged = (repair_tag(str(title[0])), ' / '.join(repair_tag(a) for a in artist))
    except Exception:
        pass
    # Both filename orders are common. Ambiguous names without a separator are not guessed.
    # Try each separator: A-Lin is an artist name, not necessarily the boundary.
    separators = list(__import__('re').finditer(r'\s+-\s+', path.stem))
    if not separators:
        separators = list(__import__('re').finditer('-', path.stem))
    for sep in separators:
        a, b = path.stem[:sep.start()].strip(), path.stem[sep.end():].strip()
        if a and b:
            result.extend([(a, b), (b, a)])
    if tagged:
        # A filename marked Live with unmarked tags must not suppress a studio track.
        filename_versions = versions(path.stem)
        if not filename_versions or filename_versions == versions(tagged[0]):
            result.insert(0, tagged)
    return result


def repair_tag(value: str) -> str:
    """Repair reversible Latin-1 mis-decoding of GBK/UTF-8; never rewrite source tags."""
    def cjk_count(text):
        return sum('\u4e00' <= ch <= '\u9fff' for ch in text)
    try:
        raw = value.encode('latin-1')
    except UnicodeError:
        return value
    candidates = [value]
    for encoding in ('utf-8', 'gb18030'):
        try:
            candidates.append(raw.decode(encoding))
        except UnicodeError:
            pass
    return max(candidates, key=cjk_count)


def scan_library(roots: list[Path], min_bytes: int = MIN_BYTES, checkpoint=lambda: None):
    from matching import identity
    index: dict[tuple, Path] = {}
    errors: list[str] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            errors.append(f'目录不存在：{root}')
            continue
        def onerror(error):
            errors.append(str(error))
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            dirs[:] = [d for d in dirs if not Path(folder, d).is_symlink() and not Path(folder, d).is_junction()]
            for name in files:
                checkpoint()
                path = Path(folder, name)
                if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    validate_audio(path, min_bytes)
                    for title, artist in file_identity(path):
                        index.setdefault(identity(title, artist), path)
                except Exception as exc:
                    errors.append(f'{path}: {exc}')
    return index, errors
