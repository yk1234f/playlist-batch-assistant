from pathlib import Path
import unicodedata
from provider import SOURCES

ALIASES = {
    'netease': ('网易', 'netease', 'cloudmusic'),
    'qq': ('qq',), 'kugou': ('酷狗', 'kugou'),
    'kuwo': ('酷我', 'kuwo'), 'migu': ('咪咕', 'migu'),
}


def filename_sources(filename):
    value = unicodedata.normalize('NFKC', Path(filename).name).casefold()
    return [source for source, names in ALIASES.items() if any(name in value for name in names)]


def selected_for(track, sources, by_filename=False):
    if track.music_source in SOURCES.values():
        return [track.music_source]
    inferred = filename_sources(track.source_file) if by_filename else []
    return inferred or list(dict.fromkeys(source for source in sources if source in SOURCES.values()))


def search_plan(track, sources, retry_sources=(), by_filename=False):
    chosen = selected_for(track, sources, by_filename)
    primary = [(source, track.query, 1) for source in chosen]
    secondary = [(source, track.title, 2) for source in chosen
                 if source in retry_sources and track.title != track.query]
    return primary + secondary
