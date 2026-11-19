"""channels.txt -> your approved channels.

Everything in your list is treated as APPROVED by you: only add channels whose
owners told you they are happy for you to make clips of their videos (keep the
email/DM as proof). To pause a channel, put a # in front of its line.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import config
import resolve

log = logging.getLogger(__name__)

CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    ref: str = ""


def parse_channel_lines(text: str) -> list[str]:
    """One channel per line; '#' starts a comment; the first word on a line is the channel."""
    refs: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            refs.append(line.split()[0])
    return refs


def _safe_resolve(fetch, ref: str):
    try:
        return fetch(ref)
    except Exception as exc:  # noqa: BLE001 - reported as a readable problem line
        return exc


def load_channels(path: Path | str, cache=None, fetch=None, workers: int = 8) -> tuple[list[Channel], list[str]]:
    """Read channels.txt (or an older channels.json). Returns (channels, problems).

    `cache` is anything with get_meta/set_meta (the State); resolved ids are remembered
    there so each @handle is only looked up once.
    """
    fetch = fetch or resolve.resolve_channel
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    problems: list[str] = []
    found: list[tuple[str, str, str]] = []           # (ref, id, name)

    if path.suffix.lower() == ".json":
        raw = json.loads(text)
        raw = raw.get("channels", []) if isinstance(raw, dict) else raw
        for index, item in enumerate(raw, start=1):
            channel_id = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
            if CHANNEL_ID_RE.match(channel_id):
                found.append((channel_id, channel_id, str(item.get("name") or channel_id)))
            else:
                problems.append(f"entry #{index}: {channel_id!r} is not a channel id, skipped")
    else:
        resolved: dict[str, tuple[str, str]] = {}
        todo: list[str] = []
        refs = parse_channel_lines(text)
        for ref in refs:
            direct = resolve.channel_id_in(ref)
            if direct:
                resolved[ref] = (direct, "")
                continue
            cached = cache.get_meta("chan:" + resolve.cache_key(ref)) if cache else None
            if cached:
                channel_id, _, name = cached.partition("|")
                resolved[ref] = (channel_id, name)
            elif ref not in todo:
                todo.append(ref)
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                results = list(pool.map(lambda r: _safe_resolve(fetch, r), todo))
            for ref, result in zip(todo, results):
                if isinstance(result, Exception):
                    problems.append(f"{ref}: {result}")
                    continue
                resolved[ref] = result
                if cache:
                    cache.set_meta("chan:" + resolve.cache_key(ref), f"{result[0]}|{result[1]}")
        for ref in refs:
            if ref in resolved:
                channel_id, name = resolved[ref]
                found.append((ref, channel_id, name or ref))

    channels: list[Channel] = []
    seen: set[str] = set()
    for ref, channel_id, name in found:
        if channel_id in seen:
            problems.append(f"{ref}: listed twice, counted once")
            continue
        seen.add(channel_id)
        channels.append(Channel(id=channel_id, name=name, ref=ref))
    if len(channels) > config.MAX_CHANNELS:
        problems.append(f"{len(channels)} channels listed; only the first {config.MAX_CHANNELS} are used")
        channels = channels[: config.MAX_CHANNELS]
    return channels, problems
