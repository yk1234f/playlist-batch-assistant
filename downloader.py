from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from audio_files import EXTENSIONS, MIN_BYTES, validate_audio
from matching import safe_stem


def audio_extension(header: bytes, url: str, content_type: str) -> str:
    # Website names all blobs .mp3. Prefer the actual signature over that label.
    if header.startswith(b'fLaC'):
        return '.flac'
    if header.startswith(b'RIFF') and header[8:12] == b'WAVE':
        return '.wav'
    if header.startswith(b'OggS'):
        return '.opus' if b'OpusHead' in header else '.ogg'
    if header[4:8] == b'ftyp':
        return '.m4a'
    if header.startswith(b'MAC '):
        return '.ape'
    if header.startswith(b'FORM'):
        return '.aiff'
    if header.startswith(bytes.fromhex('3026b2758e66cf11')):
        return '.wma'
    if len(header) > 1 and header[0] == 255 and header[1] & 246 == 240:
        return '.aac'
    if header.startswith(b'ID3') or (len(header) > 1 and header[0] == 255 and header[1] & 224 == 224):
        return '.mp3'
    raise ValueError('响应没有可识别的音频文件头')


def download_audio(provider, candidate, track, directory: Path, checkpoint=lambda: None,
                   min_bytes: int = MIN_BYTES, max_bytes: int = 512 * 1024 * 1024,
                   max_seconds: float = 180):
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f'.{uuid.uuid4().hex}.part'
    started = time.monotonic()
    try:
        checkpoint()
        with provider.open(candidate.url) as response, temp.open('xb') as out:
            if response.status != 200:
                raise ValueError(f'下载 HTTP 状态异常：{response.status}')
            content_type = response.headers.get('Content-Type', '')
            declared = response.headers.get('Content-Length')
            expected = int(declared) if declared is not None else None
            if expected is not None and (expected < min_bytes or expected > max_bytes):
                raise ValueError('服务器声明的文件大小超出限制')
            size = 0
            extension = None
            while True:
                checkpoint()
                if time.monotonic() - started > max_seconds:
                    raise TimeoutError('单首下载超过时间限制')
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                if extension is None:
                    extension = audio_extension(chunk, response.url, content_type)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('文件超过最大下载大小')
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if expected is not None and size != expected:
            raise ValueError('实际文件大小与 Content-Length 不一致')
        if extension is None:
            raise ValueError('空下载文件')
        info = validate_audio(temp, min_bytes, extension, content_type)
        if candidate.duration and info.duration < candidate.duration * .9:
            raise ValueError('音频时长明显短于搜索结果，可能为试听片段')
        checkpoint()
        stem = safe_stem(track.title, track.artist)
        # Windows rename fails if a destination exists. POSIX hard links give
        # the same atomic, no-overwrite behavior (rename would overwrite there).
        for i in range(1, 10000):
            destination = directory / (stem + (f' ({i})' if i > 1 else '') + extension)
            try:
                if os.name == 'nt':
                    os.rename(temp, destination)
                else:
                    os.link(temp, destination)
                return destination, info
            except FileExistsError:
                continue
        raise FileExistsError('同名文件过多')
    finally:
        temp.unlink(missing_ok=True)
