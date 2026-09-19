"""Adapter for the public form used by jbsou.cn/static/js/music.js (2026-09-19)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from http.cookiejar import CookieJar

BASE_URL = 'https://www.jbsou.cn/'
SOURCES = {'网易': 'netease', 'QQ': 'qq', '酷狗': 'kugou', '酷我': 'kuwo', '咪咕': 'migu'}


class ProviderError(RuntimeError):
    pass


@dataclass
class Candidate:
    title: str
    artist: str
    url: str
    source: str
    page_url: str
    duration: float = 0


def http_url(value: str, base: str = BASE_URL) -> str:
    url = urllib.parse.urljoin(base, value)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError('下载地址不是普通 HTTP/HTTPS 地址')
    return url


class JBSou:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 25):
        self.base_url = http_url(base_url)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def open(self, url: str, data=None):
        req = urllib.request.Request(http_url(url, self.base_url), data=data, headers={
            'User-Agent': 'Mozilla/5.0 PlaylistBatchAssistant/2.0',
            'Referer': self.base_url,
            'Accept-Encoding': 'identity',
            **({'X-Requested-With': 'XMLHttpRequest'} if data is not None else {}),
        })
        try:
            return self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise ProviderError(f'网站返回 HTTP {exc.code}，请稍后或打开页面检查') from exc
            raise ProviderError(f'HTTP {exc.code}') from exc

    def search(self, query: str, source: str, page: int = 1) -> list[Candidate]:
        form = urllib.parse.urlencode({'input': query, 'filter': 'name', 'type': source, 'page': page}).encode()
        with self.open(self.base_url, form) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ProviderError('搜索响应过大')
        try:
            result = json.loads(body.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise ProviderError('搜索接口未返回 JSON，可能是验证页面或网站改版') from exc
        if not isinstance(result, dict):
            raise ProviderError('搜索响应结构已变化')
        if result.get('code') == 404:
            return []
        if result.get('code') != 200 or not isinstance(result.get('data'), list):
            raise ProviderError(str(result.get('error', '搜索接口异常'))[:200])
        candidates = []
        page_url = self.base_url + '?' + urllib.parse.urlencode({'name': query, 'type': source})
        for item in result['data']:
            if not isinstance(item, dict) or not all(item.get(k) for k in ('name', 'artist', 'url')):
                continue
            artist = item['artist']
            if isinstance(artist, list):
                artist = ' / '.join(str(a) for a in artist)
            candidates.append(Candidate(str(item['name']), str(artist), http_url(str(item['url']), self.base_url), source, page_url))
        return candidates
