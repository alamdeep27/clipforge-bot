"""Thin wrapper around the yt-dlp command line (invoked as `python -m yt_dlp`).

Running it through the current interpreter means cron jobs use the .venv copy of
yt-dlp without needing the venv's bin directory on PATH.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys

import config

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_URL_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([0-9A-Za-z_-]{11})")


class YtDlpError(RuntimeError):
    """yt-dlp failed; the message is the last lines of its stderr."""


def video_id_from(ref: str) -> str:
    """Accept a bare 11-character id or any common YouTube URL."""
    ref = ref.strip()
    if _ID_RE.match(ref):
        return ref
    match = _URL_ID_RE.search(ref)
    if not match:
        raise ValueError(f"cannot find a YouTube video id in {ref!r}")
    return match.group(1)


def video_url(ref: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id_from(ref)}"


def base_cmd() -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings"]
    if config.COOKIES_FILE:
        cmd += ["--cookies", config.COOKIES_FILE]
    if config.JS_RUNTIMES:
        cmd += ["--js-runtimes", config.JS_RUNTIMES]
    return cmd


def run(args, timeout: float) -> subprocess.CompletedProcess:
    cmd = base_cmd() + [str(a) for a in args]
    log.debug("running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise YtDlpError(f"yt-dlp timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or proc.stdout or "").strip().splitlines() if ln.strip()]
        raise YtDlpError(" | ".join(lines[-3:]) or f"yt-dlp exited with code {proc.returncode}")
    return proc


def fetch_info(ref: str, extra=(), timeout: float = 180) -> dict:
    """`yt-dlp --dump-json --no-download` for one video, parsed."""
    proc = run(["--dump-json", "--no-download", *extra, video_url(ref)], timeout)
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise YtDlpError("yt-dlp returned no JSON metadata")
