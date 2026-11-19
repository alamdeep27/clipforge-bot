"""clipper.py - cut the chosen window and turn it into a captioned 1080x1920 YouTube Short.

* yt-dlp downloads ONLY the target time window (--download-sections).
* captions.py turns the speech into subtitles (free, on the CPU).
* ffmpeg builds the vertical frame (blurred-background layout or centred 9:16
  crop), burns in the captions, normalises loudness (two-pass loudnorm) and keeps
  the length below 59 s.
* Raw temp files are deleted the moment the clip is rendered (or fails).
"""
from __future__ import annotations

import json
import logging
import math
import re
import subprocess
from pathlib import Path

import captions
import config
import ytdlp
from detector import Window

log = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920
# Prefer <=1080p video + audio; fall back to a single combined format.
SOURCE_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


class ClipError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# ffmpeg / ffprobe helpers
# --------------------------------------------------------------------------
def _ffmpeg(args: list[str], timeout: float = 1800, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise ClipError("ffmpeg not found on PATH - install it first (see START-HERE.md)") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        tail = " | ".join([ln for ln in proc.stderr.strip().splitlines() if ln.strip()][-4:])
        raise ClipError(f"ffmpeg failed: {tail}")
    return proc


def probe(path: Path) -> dict:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except FileNotFoundError as exc:
        raise ClipError("ffprobe not found on PATH - install ffmpeg (see START-HERE.md)") from exc
    if proc.returncode != 0:
        raise ClipError(f"ffprobe could not read {path}: {proc.stderr.strip()[-200:]}")
    return json.loads(proc.stdout)


def duration_of(meta: dict) -> float:
    return float(meta["format"]["duration"])


def has_audio(meta: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in meta.get("streams", []))


def video_size(meta: dict) -> tuple[int, int]:
    for s in meta.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise ClipError("no video stream found")


# --------------------------------------------------------------------------
# Download only the target window
# --------------------------------------------------------------------------
def download_section(ref: str, start: float, end: float, work_dir: Path) -> Path:
    vid = ytdlp.video_id_from(ref)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        ytdlp.run(
            [
                "-f", SOURCE_FORMAT,
                "--download-sections", f"*{start:.2f}-{end:.2f}",
                "--force-keyframes-at-cuts",
                "--merge-output-format", "mkv",
                "--quiet", "--no-progress",
                "-o", str(work_dir / f"{vid}_raw.%(ext)s"),
                ytdlp.video_url(ref),
            ],
            timeout=1800,
        )
    except ytdlp.YtDlpError as exc:
        raise ClipError(f"section download failed: {exc}") from exc
    files = [
        p for p in work_dir.glob(f"{vid}_raw*")
        if p.suffix.lower() in (".mkv", ".mp4", ".webm", ".mov")
    ]
    if not files:
        raise ClipError("yt-dlp finished but no video file was produced")
    return max(files, key=lambda p: p.stat().st_mtime)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
def video_filter(layout: str, ass_name: str | None = None) -> str:
    """Filter graph that ends in a labelled 1080x1920 stream called [v].

    ass_name is a subtitle file name relative to ffmpeg's working directory
    (render() runs ffmpeg inside the work folder, so no path escaping is needed,
    even on Windows). Fonts are read from the ./fonts folder next to it.
    """
    if layout == "crop":
        # Centred 9:16 slice of the frame, scaled up. Biggest picture, loses the sides.
        base = f"[0:v]crop='min(iw,ih*9/16)':ih,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1"
    else:
        # "blur": whole frame centred over a blurred, zoomed copy of itself.
        # The background is blurred at 1/4 size (cheap) and scaled back up.
        base = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=6,"
            f"scale={OUT_W}:{OUT_H},eq=brightness=-0.06:saturation=1.15[bgb];"
            f"[fg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
            "flags=lanczos,setsar=1[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    subs = f",subtitles={ass_name}:fontsdir=fonts" if ass_name else ""
    return f"{base}{subs},format=yuv420p[v]"


def measure_loudness(src: Path, start: float, duration: float) -> dict | None:
    """First loudnorm pass. Returns the measured values, or None (silent/unmeasurable audio)."""
    af = f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json"
    try:
        proc = _ffmpeg(
            ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src), "-vn", "-af", af, "-f", "null", "-"]
        )
    except ClipError as exc:
        log.warning("loudness measurement failed (%s); using single-pass loudnorm", exc)
        return None
    match = _LOUDNORM_JSON.search(proc.stderr)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        values = {k: float(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (KeyError, ValueError):
        return None
    return values if all(math.isfinite(v) for v in values.values()) else None


def audio_filter(measured: dict | None) -> str:
    base = f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11"
    if measured:
        base += (
            f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}:linear=true"
        )
    return base + ",aresample=48000"          # loudnorm resamples internally; restore 48 kHz


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
def render(
    src: Path,
    out: Path,
    start: float,
    duration: float,
    layout: str,
    measured: dict | None,
    ass: Path | None = None,
) -> Path:
    src, out = Path(src).resolve(), Path(out).resolve()
    meta = probe(src)
    cmd = [
        "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
        "-filter_complex", video_filter(layout, ass.name if ass else None), "-map", "[v]",
    ]
    if has_audio(meta):
        cmd += ["-map", "0:a:0", "-af", audio_filter(measured), "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        log.warning("source has no audio track; rendering a silent Short")
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", "21",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
        str(out),
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(cmd, cwd=ass.parent if ass else None)

    check = probe(out)
    width, height = video_size(check)
    length = duration_of(check)
    if (width, height) != (OUT_W, OUT_H):
        raise ClipError(f"rendered {width}x{height}, expected {OUT_W}x{OUT_H}")
    if length >= 59.0:
        raise ClipError(f"rendered clip is {length:.1f}s; Shorts must stay under 59s")
    return out


def make_short(
    ref: str,
    window: Window,
    out_path: Path,
    *,
    work_dir: Path,
    layout: str | None = None,
    local_media: Path | None = None,
    caption_words: int | None = None,
    caption_upper: bool | None = None,
) -> Path:
    """Download (or open) the window, caption it, render the Short, delete raw temp files."""
    layout = layout or config.LAYOUT
    if layout not in ("blur", "crop"):
        raise ClipError(f"unknown layout {layout!r} (use 'blur' or 'crop')")
    vid = "local" if local_media is not None else ytdlp.video_id_from(ref)
    duration = min(window.duration, config.CLIP_MAX_SECONDS)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw: Path | None = None
    try:
        if local_media is None:
            raw = download_section(ref, window.start, window.end, work_dir)
            src, offset = raw, 0.0                      # the download already starts at the window
        else:
            src, offset = Path(local_media), window.start   # local test file: seek inside it

        ass = None
        try:
            ass = captions.make_for_clip(src, offset, duration, work_dir, caption_words, caption_upper)
        except Exception as exc:  # noqa: BLE001 - a Short without captions beats no Short
            log.warning("captions skipped: %s", exc)

        measured = measure_loudness(src, offset, duration)
        try:
            render(src, out_path, offset, duration, layout, measured, ass)
        except ClipError as exc:
            if ass is None:
                raise
            log.warning("render with captions failed (%s); retrying without captions", exc)
            render(src, out_path, offset, duration, layout, measured, None)
        log.info("rendered %s (%.1fs, %s layout, captions %s)", out_path.name, duration, layout, "on" if ass else "off")
        return out_path
    finally:
        # "Delete temporary raw video files immediately after clipping."
        if raw is not None:
            raw.unlink(missing_ok=True)
        if work_dir.exists():
            for leftover in work_dir.glob(f"{vid}_raw*"):
                leftover.unlink(missing_ok=True)
