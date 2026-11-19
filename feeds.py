"""Zero-quota polling: YouTube's public per-channel Atom/RSS feed.

    https://www.youtube.com/feeds/videos.xml?channel_id=UC...

No API key, no quota. Standard library only (urllib + xml.etree).
"""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
USER_AGENT = "Mozilla/5.0 (compatible; shorts-pipeline/1.0)"
_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


class FeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    channel_id: str
    title: str
    published: str
    author: str
    views: int = 0            # the feed shows public view and like counts: free performance data
    likes: int = 0
    description: str = ""


def _count(node, attribute: str) -> int:
    try:
        return int(node.get(attribute)) if node is not None else 0
    except (TypeError, ValueError):
        return 0


def parse_feed(xml_bytes: bytes, fallback_channel_id: str = "") -> list[FeedEntry]:
    root = ET.fromstring(xml_bytes)
    feed_channel = root.findtext("yt:channelId", default=fallback_channel_id, namespaces=_NS)
    entries: list[FeedEntry] = []
    for entry in root.findall("a:entry", _NS):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=_NS) or "").strip()
        if not video_id:
            continue
        entries.append(
            FeedEntry(
                video_id=video_id,
                channel_id=entry.findtext("yt:channelId", default=feed_channel, namespaces=_NS),
                title=(entry.findtext("a:title", default="", namespaces=_NS) or "").strip(),
                published=(entry.findtext("a:published", default="", namespaces=_NS) or "").strip(),
                author=(entry.findtext("a:author/a:name", default="", namespaces=_NS) or "").strip(),
                views=_count(entry.find("media:group/media:community/media:statistics", _NS), "views"),
                likes=_count(entry.find("media:group/media:community/media:starRating", _NS), "count"),
                description=entry.findtext("media:group/media:description", default="", namespaces=_NS) or "",
            )
        )
    return entries


def fetch_feed(channel_id: str, timeout: float = 20.0, retries: int = 2) -> list[FeedEntry]:
    url = FEED_URL.format(channel_id=channel_id)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return parse_feed(response.read(), channel_id)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 404):      # wrong or removed channel id: retrying will not help
                break
        except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise FeedError(f"{channel_id}: {last}")


def poll_all(channel_ids: list[str], workers: int = 8) -> dict[str, list[FeedEntry] | FeedError]:
    """Fetch every feed concurrently. Failures are returned, not raised."""

    def one(channel_id: str):
        try:
            return channel_id, fetch_feed(channel_id)
        except FeedError as exc:
            return channel_id, exc

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(pool.map(one, channel_ids))
