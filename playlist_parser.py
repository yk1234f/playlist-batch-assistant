from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Tuple
from matching import identity

KNOWN_FLAGS = ("VIP专享", "下架/无版权", "付费专辑")
FLAG_RE = re.compile(r"\[(VIP专享|下架/无版权|付费专辑)\]")


@dataclass
class Track:
    uid: str
    title: str
    artist: str
    album: str = ""
    flags: str = ""
    source_file: str = ""
    source_line: int = 0
    status: str = "待处理"
    note: str = ""

    @property
    def query(self) -> str:
        return f"{self.title} {self.artist}".strip()

    def to_dict(self):
        return asdict(self)


def read_text_safely(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _clean(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _extract_flags(*values: str) -> Tuple[str, list[str]]:
    found: list[str] = []
    cleaned: list[str] = []
    for value in values:
        text = value or ""
        for flag in KNOWN_FLAGS:
            if f"[{flag}]" in text and flag not in found:
                found.append(flag)
        text = FLAG_RE.sub("", text)
        cleaned.append(_clean(text))
    return " / ".join(found), cleaned


def parse_txt(path: Path) -> List[Track]:
    text = read_text_safely(path)
    lines = text.splitlines()
    preview = "\n".join(lines[:20])
    is_playlistout = "PlaylistOut" in preview or "歌曲总数:" in preview or "歌单名称:" in preview

    tracks: list[Track] = []
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or set(line) == {"="}:
            continue
        if is_playlistout and re.match(r"^(创建时间|导出时间|导出工具|歌单名称|歌单作者|歌曲总数|歌单链接)\s*:", line):
            continue

        if " - " not in line:
            continue

        if is_playlistout:
            parts = line.split(" - ", 2)
            if len(parts) < 2:
                continue
            title = parts[0]
            artist = parts[1]
            album = parts[2] if len(parts) >= 3 else ""
        else:
            title, artist = line.split(" - ", 1)
            album = ""

        flags, cleaned = _extract_flags(title, artist, album)
        title, artist, album = cleaned
        if not title or not artist:
            continue
        tracks.append(
            Track(
                uid=hashlib.sha256(f'{path.resolve()}:{lineno}'.encode()).hexdigest()[:24],
                title=title,
                artist=artist,
                album=album,
                flags=flags,
                source_file=path.name,
                source_line=lineno,
            )
        )
    return tracks


def parse_csv(path: Path) -> List[Track]:
    text = read_text_safely(path)
    rows = csv.DictReader(text.splitlines())
    tracks: list[Track] = []
    if not rows.fieldnames:
        return tracks

    field_map = {name.lower().strip(): name for name in rows.fieldnames if name}
    title_key = field_map.get("title") or field_map.get("歌名") or field_map.get("歌曲")
    artist_key = field_map.get("artist") or field_map.get("歌手") or field_map.get("艺人")
    album_key = field_map.get("album") or field_map.get("专辑")
    if not title_key or not artist_key:
        return tracks

    for idx, row in enumerate(rows, start=2):
        title = row.get(title_key, "")
        artist = row.get(artist_key, "")
        album = row.get(album_key, "") if album_key else ""
        flags, cleaned = _extract_flags(title, artist, album)
        title, artist, album = cleaned
        if not title or not artist:
            continue
        tracks.append(
            Track(
                uid=hashlib.sha256(f'{path.resolve()}:{idx}'.encode()).hexdigest()[:24],
                title=title,
                artist=artist,
                album=album,
                flags=flags,
                source_file=path.name,
                source_line=idx,
            )
        )
    return tracks


def parse_file(path: str | Path) -> List[Track]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".txt", ".list"}:
        return parse_txt(path)
    if suffix == ".csv":
        return parse_csv(path)
    raise ValueError(f"不支持的文件类型: {path.suffix}")


def _norm_title(value: str) -> str:
    value = _clean(value).lower()
    value = value.replace("（", "(").replace("）", ")")
    value = re.sub(r"\s*([()])\s*", r"\1", value)
    return value


def _norm_artist(value: str) -> str:
    value = _clean(value).lower()
    parts = re.split(r"\s*(?:/|／|,|，|、|&|＆|feat\.?|featuring)\s*", value, flags=re.I)
    parts = sorted({p.strip() for p in parts if p.strip()})
    return "|".join(parts)


def dedupe_tracks(tracks: Iterable[Track]) -> Tuple[List[Track], List[Track]]:
    seen: dict[tuple[str, str], Track] = {}
    unique: list[Track] = []
    dupes: list[Track] = []
    for track in tracks:
        key = identity(track.title, track.artist)
        if key in seen:
            dupes.append(track)
            continue
        seen[key] = track
        unique.append(track)
    return unique, dupes


def merge_files(paths: Iterable[str | Path]) -> Tuple[List[Track], List[Track]]:
    all_tracks: list[Track] = []
    for path in paths:
        all_tracks.extend(parse_file(path))
    return dedupe_tracks(all_tracks)
