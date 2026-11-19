"""delivery.py - what happens to a finished Short.

files (default): move it into READY_DIR next to a text file with the title,
                 description and tags, ready to paste into YouTube.
api            : upload it through the YouTube Data API (see uploader.py).
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import config
import uploader
from db import State
from permissions import Channel

if TYPE_CHECKING:
    from detector import Window

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Delivered:
    kind: str           # "ready" or "uploaded"
    where: str          # file path or YouTube link
    title: str = ""     # the title that was generated (used to find the video on your channel later)
    video_id: str = ""  # YouTube id of the upload (api mode only)


def slug(text: str | None, limit: int = 40) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-").lower()
    return folded[:limit].strip("-") or "short"


def sidecar_text(body: dict, info: dict, hint: str = "") -> str:
    snippet = body["snippet"]
    return hint + (
        "TITLE (paste into YouTube)\n"
        f"{snippet['title']}\n\n"
        "DESCRIPTION (paste into YouTube)\n"
        f"{snippet['description']}\n\n"
        f"TAGS: {', '.join(snippet['tags'])}\n"
        f"SOURCE VIDEO: https://www.youtube.com/watch?v={info['id']}\n"
    )


def cleanup_ready(days: int | None = None) -> int:
    """Delete finished files older than READY_KEEP_DAYS so the folder never fills the disk."""
    days = config.READY_KEEP_DAYS if days is None else days
    if days <= 0 or not config.READY_DIR.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in config.READY_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in (".mp4", ".txt") and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def deliver_files(
    state: State, clip: Path, info: dict, channel: Channel, window: "Window",
    privacy: str | None = None, title_style: str = "plain",
) -> Delivered:
    allowed, why = uploader.cap_status(state)
    if not allowed:
        raise uploader.DailyCapReached(why)
    body = uploader.build_body(info, channel, window, privacy, title_style)
    config.READY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = f"{stamp}_{slug(info.get('title'))}_{info['id']}"
    target = config.READY_DIR / f"{base}.mp4"
    shutil.move(str(clip), str(target))
    hint = ""
    try:
        import learner

        hint = learner.post_time_hint(state)
    except Exception:  # noqa: BLE001 - the hint is a bonus, never a reason to lose a Short
        pass
    (config.READY_DIR / f"{base}.txt").write_text(sidecar_text(body, info, hint), encoding="utf-8")
    state.record_upload(info["id"], "", status="ready", detail=f"ready: {target.name}")
    return Delivered("ready", str(target), title=body["snippet"]["title"])


def deliver(
    state: State, clip: Path, info: dict, channel: Channel, window: "Window",
    privacy: str | None = None, title_style: str = "plain",
) -> Delivered:
    if config.DELIVERY == "api":
        result = uploader.upload_clip(state, clip, info, channel, window, privacy=privacy, title_style=title_style)
        return Delivered("uploaded", result.url, title=result.title, video_id=result.video_id)
    return deliver_files(state, clip, info, channel, window, privacy, title_style)
