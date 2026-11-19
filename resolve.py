"""resolve.py - turn a channel link, @handle or UC... id into a channel id. No API key needed."""
from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UC_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
_CANONICAL = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"')
_ITEMPROP = re.compile(r'<meta itemprop="(?:channelId|identifier)" content="(UC[0-9A-Za-z_-]{22})"')
_EXTERNAL = re.compile(r'"externalId":"(UC[0-9A-Za-z_-]{22})"')
_OG_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_PAGE_SUFFIX = re.compile(
    r"/(videos|shorts|streams|live|featured|about|playlists|community|channels|releases|podcasts|store)/?$", re.I
)
_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


class ResolveError(RuntimeError):
    pass


def channel_id_in(ref: str) -> str | None:
    """The UC... id when the text already contains one (bare id or a /channel/UC... link)."""
    match = UC_RE.search(ref)
    if match and (ref.strip() == match.group(0) or "/channel/" in ref):
        return match.group(0)
    return None


def to_url(ref: str) -> str:
    ref = ref.strip()
    if not ref:
        raise ResolveError("empty line")
    if ref.startswith("@"):
        return f"https://www.youtube.com/{ref}"
    if "://" not in ref and "youtube.com" in ref:
        ref = "https://" + ref
    if "://" in ref:
        parts = urllib.parse.urlsplit(ref)
        host = parts.netloc.lower()
        if host not in _HOSTS:
            raise ResolveError("this is not a youtube.com channel link")
        path = parts.path.rstrip("/")
        if path.startswith(("/watch", "/shorts/", "/live/", "/playlist")) or not path:
            raise ResolveError("this is a video/playlist link - paste the CHANNEL link (it looks like youtube.com/@name)")
        return f"https://www.youtube.com{_PAGE_SUFFIX.sub('', path)}"
    return f"https://www.youtube.com/@{ref.lstrip('@')}"


def cache_key(ref: str) -> str:
    try:
        return to_url(ref).lower()
    except ResolveError:
        return ref.strip().lower()


def parse_page(page: str) -> tuple[str, str]:
    """(channel id, channel name) from a channel page's HTML."""
    for pattern in (_CANONICAL, _ITEMPROP, _EXTERNAL):
        match = pattern.search(page)
        if match:
            title = _OG_TITLE.search(page)
            return match.group(1), html.unescape(title.group(1)).strip() if title else ""
    raise ResolveError("could not find the channel id on that page")


def fetch_page(url: str, timeout: float = 20.0, retries: int = 2) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI",          # skips the EU cookie wall
    }
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(3_000_000).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 404):
                raise ResolveError("YouTube says that channel does not exist - check the link") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise ResolveError(f"could not open the channel page ({last})")


def resolve_channel(ref: str) -> tuple[str, str]:
    """Return (channel_id, name) for a channel link, @handle or UC... id."""
    direct = channel_id_in(ref)
    if direct:
        return direct, ""
    return parse_page(fetch_page(to_url(ref)))
