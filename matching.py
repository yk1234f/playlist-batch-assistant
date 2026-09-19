"""Conservative, deterministic identity and matching; no version guessing."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


def normalize(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold().strip()


def compact(value: str) -> str:
    return re.sub(r'[\W_]+', '', normalize(value))


VERSION_PATTERNS = {
    'live': r'(?<![a-z])live(?:版)?(?![a-z])|现场(?:版)?',
    'remix': r'remix(?:版)?|混音(?:版)?',
    'instrumental': r'\binstrumental\b|伴奏|纯音乐',
    'acoustic': r'\bacoustic\b|不插电',
    'demo': r'\bdemo\b',
    'cover': r'\bcover\b|翻唱',
    'remaster': r'\bremaster(?:ed)?\b|重制(?:版)?',
}


def versions(title: str) -> tuple[str, ...]:
    value = normalize(title)
    result = [label for label, pattern in VERSION_PATTERNS.items() if re.search(pattern, value)]
    result += re.findall(r'\d+(?:\.\d+)?\s*x\b|加速|降速|升调|降调', value)
    return tuple(sorted(result))


def base_title(title: str) -> str:
    value = normalize(title)
    for pattern in VERSION_PATTERNS.values():
        value = re.sub(pattern, '', value)
    return compact(value)


def artists(value: str) -> tuple[str, ...]:
    parts = re.split(r'\s*(?:/|,|、|&|;|\bfeat\.?\s|\bfeaturing\s)\s*', normalize(value))
    return tuple(sorted({compact(p) for p in parts if compact(p)}))


def identity(title: str, artist: str) -> tuple:
    # Preserve all edition subtitles, not just the Live/Remix category.
    return base_title(title), artists(artist), versions(title)


@dataclass(frozen=True)
class Match:
    title: float
    artist: float
    version: bool
    automatic: bool

    @property
    def score(self) -> float:
        return (self.title * .6 + self.artist * .4) if self.version else 0


def compare(title: str, artist: str, other_title: str, other_artist: str) -> Match:
    left, right = base_title(title), base_title(other_title)
    ts = SequenceMatcher(None, left, right).ratio() if left and right else 0
    a, b = artists(artist), artists(other_artist)
    similarity = SequenceMatcher(None, '|'.join(a), '|'.join(b)).ratio() if a and b else 0
    vs = versions(title) == versions(other_title)
    # Fuzzy scores help review. Only exact normalized identity auto-downloads.
    return Match(ts, similarity, vs, bool(ts == 1 and a == b and a and vs))


def safe_stem(title: str, artist: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', f'{title} - {artist}').strip(' .')[:160].rstrip(' .')
    if not value or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    return value
