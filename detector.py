"""detector.py - choose the hottest 45-58 second window of a video.

1. Primary: YouTube's "Most Replayed" heatmap (info["heatmap"]) -> highest peak.
2. Fallback (heatmap not populated yet, typical for brand-new uploads): loudness
   spikes in the audio track, boosted by timestamp mentions in comments and by
   live-chat replay density when those exist.

Both paths end the same way: find the centre of the hottest moment, then place
a clip window around it, inside the video and away from the intro/outro.
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import config
import ytdlp

log = logging.getLogger(__name__)

HOP_SECONDS = 0.5            # resolution of the energy curve
SMOOTH_SECONDS = 8.0         # a "moment" must be sustained, not a single loud pop
ANALYSIS_RATE = 8000         # Hz, mono; plenty for loudness
EDGE_MARGIN = 2.0            # never end a clip in the last two seconds of the video
COMMENT_MIN_COUNT = 25       # only read comments on videos that have some
COMMENT_WEIGHT = 0.6
CHAT_WEIGHT = 0.8

_TS_RE = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")


class DetectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    method: str       # "heatmap", "audio", "audio+comments", ...
    peak: float       # centre of the hottest moment, in source seconds
    score: float

    @property
    def duration(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------
# Window placement (shared by both detection paths)
# --------------------------------------------------------------------------
def clamp_length(length: float | None = None) -> float:
    length = config.CLIP_TARGET_SECONDS if length is None else length
    return max(config.CLIP_MIN_SECONDS, min(float(length), config.CLIP_MAX_SECONDS))


def place_window(
    center: float,
    duration: float,
    length: float | None = None,
    min_start: float | None = None,
) -> tuple[float, float] | None:
    """Centre a clip on `center`, clamped inside the video.

    The first `min_start` seconds (intro) are skipped when the video is long enough
    to allow it. Returns None when the video cannot hold a minimum-length clip.
    """
    length = clamp_length(length)
    min_start = config.SKIP_INTRO_SECONDS if min_start is None else min_start
    latest_end = duration - EDGE_MARGIN
    earliest_start = min_start if latest_end - min_start >= length else 0.0
    if latest_end - earliest_start < length:
        length = latest_end - earliest_start          # use whatever room there is...
        if length < config.CLIP_MIN_SECONDS:          # ...unless it is too short to be worth it
            return None
    start = center - length / 2.0
    start = max(earliest_start, min(start, latest_end - length))
    return round(start, 2), round(start + length, 2)


# --------------------------------------------------------------------------
# Primary: "Most Replayed" heatmap
# --------------------------------------------------------------------------
def heatmap_window(info: dict, length: float | None = None) -> Window | None:
    heat = info.get("heatmap")
    duration = float(info.get("duration") or 0)
    if not heat or duration <= 0:
        return None

    segments: list[tuple[float, float, float]] = []
    for seg in heat:
        try:
            segments.append((float(seg["start_time"]), float(seg["end_time"]), float(seg["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not segments:
        return None

    # The curve almost always peaks in the intro, and the outro is end-screen noise.
    eligible = [
        s for s in segments
        if config.SKIP_INTRO_SECONDS <= (s[0] + s[1]) / 2.0 <= duration - config.SKIP_OUTRO_SECONDS
    ]
    start_s, end_s, value = max(eligible or segments, key=lambda s: s[2])
    peak = (start_s + end_s) / 2.0
    placed = place_window(peak, duration, length)
    if placed is None:
        return None
    return Window(placed[0], placed[1], "heatmap", peak, value)


# --------------------------------------------------------------------------
# Fallback: audio energy (+ comment timestamps, + live-chat density)
# --------------------------------------------------------------------------
def rms_curve(path: Path, rate: int = ANALYSIS_RATE, hop: float = HOP_SECONDS) -> np.ndarray:
    """Streaming RMS per `hop` seconds via ffmpeg (constant memory, any video length)."""
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
    ]
    chunk = int(rate * hop) * 2
    values: list[float] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(chunk)
            if not buf:
                break
            buf = buf[: len(buf) - (len(buf) % 2)]
            samples = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            values.append(float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0)
    finally:
        proc.stdout.close()
        proc.wait()
    if not values:
        raise DetectorError(f"could not decode any audio from {path}")
    return np.asarray(values, dtype=np.float32)


def hype_curve(rms: np.ndarray) -> np.ndarray:
    """Loudness above the video's own baseline, in dB (never negative)."""
    db = 20.0 * np.log10(np.maximum(rms, 1e-5))
    return np.maximum(db - np.percentile(db, 50), 0.0)


def smooth(x: np.ndarray, seconds: float = SMOOTH_SECONDS, hop: float = HOP_SECONDS) -> np.ndarray:
    k = max(1, int(round(seconds / hop)))
    return np.convolve(x, np.ones(k) / k, mode="same")


def _normalise(x: np.ndarray) -> np.ndarray:
    top = float(x.max()) if x.size else 0.0
    return x / top if top > 0 else x


def timestamps_in(text: str) -> list[int]:
    """Seconds for every m:ss / h:mm:ss mention in a comment."""
    found: list[int] = []
    for m in _TS_RE.finditer(text or ""):
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        if c is not None:
            if b < 60 and int(c) < 60:
                found.append(a * 3600 + b * 60 + int(c))
        elif b < 60:
            found.append(a * 60 + b)
    return found


def comment_curve(comments: list[dict], n: int, duration: float) -> np.ndarray:
    """Comment timestamp mentions ('3:42 insane!!') spread over +/-5 s, weighted by likes."""
    counts = np.zeros(n, dtype=np.float32)
    spread = int(5.0 / HOP_SECONDS)
    for comment in comments:
        weight = 1.0 + math.log1p(max(int(comment.get("like_count") or 0), 0))
        for t in timestamps_in(comment.get("text") or ""):
            if 0 <= t <= duration:
                i = int(t / HOP_SECONDS)
                counts[max(0, i - spread): i + spread + 1] += weight
    return counts


def chat_curve(live_chat_file: Path, n: int) -> np.ndarray:
    """Messages per hop from a yt-dlp live-chat replay file (one JSON object per line)."""
    counts = np.zeros(n, dtype=np.float32)
    with open(live_chat_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                action = json.loads(line).get("replayChatItemAction") or {}
                t = int(action.get("videoOffsetTimeMsec")) / 1000.0
            except (ValueError, TypeError, AttributeError):
                continue
            i = int(t / HOP_SECONDS)
            if 0 <= i < n:
                counts[i] += 1
    return counts


def window_from_signals(
    rms: np.ndarray,
    duration: float,
    comment_counts: np.ndarray | None = None,
    chat_counts: np.ndarray | None = None,
    length: float | None = None,
) -> Window:
    combined = _normalise(smooth(hype_curve(rms)))
    method = "audio"
    if comment_counts is not None and comment_counts.max() > 0:
        combined = combined + COMMENT_WEIGHT * _normalise(smooth(comment_counts))
        method += "+comments"
    if chat_counts is not None and chat_counts.max() > 0:
        combined = combined + CHAT_WEIGHT * _normalise(smooth(chat_counts))
        method += "+chat"

    t = (np.arange(combined.size) + 0.5) * HOP_SECONDS
    mask = (t >= config.SKIP_INTRO_SECONDS) & (t <= duration - config.SKIP_OUTRO_SECONDS)
    if not mask.any():
        mask = np.ones_like(mask, dtype=bool)
    masked = np.where(mask, combined, -np.inf)
    index = int(np.argmax(masked))
    peak, score = float(t[index]), float(combined[index])
    if score <= 0:                                  # silent video: no signal, aim for the middle
        peak, method = duration / 2.0, "middle"
    placed = place_window(peak, duration, length)
    if placed is None:
        raise DetectorError("video is too short to hold a clip")
    return Window(placed[0], placed[1], method, peak, score)


def _download_audio(ref: str, work_dir: Path) -> Path:
    vid = ytdlp.video_id_from(ref)
    work_dir.mkdir(parents=True, exist_ok=True)
    ytdlp.run(
        [
            "-f", "bestaudio[abr<=96]/bestaudio/worst",
            "--quiet", "--no-progress",
            "-o", str(work_dir / f"{vid}_audio.%(ext)s"),
            ytdlp.video_url(ref),
        ],
        timeout=1800,
    )
    files = [p for p in work_dir.glob(f"{vid}_audio.*") if p.suffix not in (".part", ".ytdl")]
    if not files:
        raise DetectorError("audio download produced no file")
    return max(files, key=lambda p: p.stat().st_mtime)


def _fetch_comments(ref: str) -> list[dict]:
    try:
        info = ytdlp.fetch_info(
            ref,
            extra=["--write-comments", "--extractor-args", "youtube:max_comments=150,150,0,0;comment_sort=top"],
            timeout=150,
        )
    except Exception as exc:                          # best effort only
        log.info("comments unavailable: %s", exc)
        return []
    return info.get("comments") or []


def _fetch_live_chat(ref: str, work_dir: Path) -> Path | None:
    vid = ytdlp.video_id_from(ref)
    try:
        ytdlp.run(
            [
                "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                "--quiet", "--no-progress",
                "-o", str(work_dir / f"{vid}.%(ext)s"),
                ytdlp.video_url(ref),
            ],
            timeout=600,
        )
    except ytdlp.YtDlpError as exc:
        log.info("live chat replay unavailable: %s", exc)
        return None
    files = list(work_dir.glob(f"{vid}*.live_chat.json"))
    return files[0] if files else None


def audio_window(
    info: dict,
    ref: str,
    work_dir: Path,
    local_media: Path | None = None,
    length: float | None = None,
) -> Window:
    duration = float(info.get("duration") or 0)
    if duration > config.MAX_SOURCE_SECONDS:
        raise DetectorError(f"video is {duration / 3600:.1f} h long; too long for the audio fallback")

    if local_media is not None:
        rms = rms_curve(local_media)
    else:
        audio = _download_audio(ref, work_dir)
        try:
            rms = rms_curve(audio)
        finally:
            audio.unlink(missing_ok=True)
    duration = duration or rms.size * HOP_SECONDS

    comment_counts = chat_counts = None
    if local_media is None:
        if int(info.get("comment_count") or 0) >= COMMENT_MIN_COUNT:
            comments = _fetch_comments(ref)
            if comments:
                comment_counts = comment_curve(comments, rms.size, duration)
        if info.get("was_live"):
            chat_file = _fetch_live_chat(ref, work_dir)
            if chat_file is not None:
                chat_counts = chat_curve(chat_file, rms.size)
    return window_from_signals(rms, duration, comment_counts, chat_counts, length)


# --------------------------------------------------------------------------
# Entry point used by main.py
# --------------------------------------------------------------------------
def find_best_window(
    info: dict,
    ref: str,
    work_dir: Path,
    local_media: Path | None = None,
    length: float | None = None,
) -> Window:
    window = heatmap_window(info, length)
    if window is not None:
        return window
    log.info("no usable heatmap yet - falling back to audio energy")
    return audio_window(info, ref, work_dir, local_media=local_media, length=length)
