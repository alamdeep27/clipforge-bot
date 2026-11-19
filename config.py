"""Central settings for the ClipForge Shorts bot.

Every value can be overridden with an environment variable, so the schedulers
(Windows Task Scheduler, cron, GitHub Actions) never need a code edit.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _path(env: str, default: Path) -> Path:
    return Path(os.environ.get(env) or default).expanduser()


def _int(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return default


def _float(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


def _flag(env: str, default: bool) -> bool:
    value = os.environ.get(env)
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


# ---- your channel (shown in every description) -------------------------------
MY_CHANNEL_NAME = os.environ.get("YT_MY_CHANNEL_NAME") or "ClipForge"
MY_CHANNEL_URL = os.environ.get("YT_MY_CHANNEL_URL") or "https://www.youtube.com/@ClipForge-n3b"
# The channel id lets the bot read your channel's public feed to see how each Short performed.
MY_CHANNEL_ID = os.environ.get("YT_MY_CHANNEL_ID") or "UC3qRO88vub1Md1CmlXoyYlg"

# ---- files and folders --------------------------------------------------------
CHANNELS_FILE = _path("YT_CHANNELS_FILE", BASE_DIR / "channels.txt")
DB_PATH = _path("YT_DB_PATH", BASE_DIR / "processed_videos.db")
TMP_DIR = _path("YT_TMP_DIR", BASE_DIR / "tmp")
OUTPUT_DIR = _path("YT_OUTPUT_DIR", BASE_DIR / "output")
READY_DIR = _path("YT_READY_DIR", OUTPUT_DIR / "ready")       # finished Shorts land here
READY_KEEP_DAYS = _int("YT_READY_KEEP_DAYS", 14)              # old finished files are tidied away
CLIENT_SECRET_FILE = _path("YT_CLIENT_SECRET", BASE_DIR / "client_secret.json")
TOKEN_FILE = _path("YT_TOKEN_FILE", BASE_DIR / "token.json")
REPORT_FILE = _path("YT_REPORT_FILE", BASE_DIR / "LEARNING-REPORT.md")
LOCK_FILE = BASE_DIR / ".pipeline.lock"
LOG_FILE = BASE_DIR / "pipeline.log"

# ---- yt-dlp -------------------------------------------------------------------
# Optional Netscape cookies file, or its text (handy for GitHub secrets).
COOKIES_FILE = os.environ.get("YT_COOKIES_FILE") or None
COOKIES_TEXT = os.environ.get("YT_COOKIES_TEXT") or None
# Only needed when you use Node/QuickJS instead of Deno, e.g. YT_JS_RUNTIMES=node
JS_RUNTIMES = os.environ.get("YT_JS_RUNTIMES") or None
# YouTube changes often and yt-dlp must keep up: the bot updates it itself about once a week.
AUTO_UPDATE = _flag("YT_AUTO_UPDATE", True)

# ---- polling ------------------------------------------------------------------
MAX_CHANNELS = 500
FEED_WORKERS = 12
MAX_VIDEO_AGE_HOURS = _int("YT_MAX_VIDEO_AGE_HOURS", 72)    # catch-up window after the PC was off
MAX_VIDEOS_PER_RUN = _int("YT_MAX_PER_RUN", 3)              # keeps a 15-min schedule from piling up
MAX_ATTEMPTS = 3

# ---- which source videos are worth clipping -------------------------------------
MIN_SOURCE_SECONDS = 120.0            # shorter than this is already a Short
MAX_SOURCE_SECONDS = 3 * 3600.0       # the audio fallback downloads the whole track

# ---- clip shape -----------------------------------------------------------------
CLIP_MIN_SECONDS = 45.0
CLIP_MAX_SECONDS = 58.0               # Shorts must stay under 60 s
CLIP_TARGET_SECONDS = _float("YT_CLIP_SECONDS", 55.0)
SKIP_INTRO_SECONDS = 15.0
SKIP_OUTRO_SECONDS = 10.0
LAYOUT = os.environ.get("YT_LAYOUT") or "blur"          # "blur" or "crop"
TARGET_LUFS = _float("YT_TARGET_LUFS", -14.0)
X264_PRESET = os.environ.get("YT_X264_PRESET") or "medium"

# ---- automatic captions (free, runs on the CPU, no account needed) ---------------
CAPTIONS = _flag("YT_CAPTIONS", True)
WHISPER_MODEL = os.environ.get("YT_WHISPER_MODEL") or "base"   # tiny / base / small (bigger = better, slower)
CAPTION_LANGUAGE = os.environ.get("YT_CAPTION_LANGUAGE") or None   # None = detect, or "en"
CAPTION_UPPER = _flag("YT_CAPTION_UPPER", True)
CAPTION_WORDS = _int("YT_CAPTION_WORDS", 3)

# ---- what happens with a finished Short -------------------------------------------
#   files : save it in READY_DIR with a ready-to-paste title/description (works today)
#   api   : upload it to YouTube by itself (only becomes public after Google approves your app)
DELIVERY = (os.environ.get("YT_DELIVERY") or "files").strip().lower()
MAX_CLIPS_PER_DAY = _int("YT_MAX_CLIPS_PER_DAY", 12)         # files mode
MAX_UPLOADS_PER_DAY = _int("YT_MAX_UPLOADS_PER_DAY", 90)     # api mode (Google's default bucket is 100)
CATEGORY_ID = "20"                                            # Gaming
PRIVACY_STATUS = os.environ.get("YT_PRIVACY") or "private"
KEEP_FINAL_CLIPS = os.environ.get("YT_KEEP_CLIPS", "0") == "1"

# ---- learning from results ---------------------------------------------------------
# The bot reads how each of your Shorts performs and shifts toward what works.
# Anything you set yourself (YT_LAYOUT, YT_CLIP_SECONDS, YT_CAPTION_WORDS, YT_CAPTION_UPPER) is left alone.
LEARNING = _flag("YT_LEARNING", True)
FIXED = {
    name
    for name, env in (
        ("layout", "YT_LAYOUT"), ("length", "YT_CLIP_SECONDS"),
        ("caption", "YT_CAPTION_WORDS"), ("caption", "YT_CAPTION_UPPER"),
    )
    if os.environ.get(env)
}
