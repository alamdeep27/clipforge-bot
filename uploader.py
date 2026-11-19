"""uploader.py - upload finished Shorts with the YouTube Data API v3 (api delivery mode).

* Login: reads client_secret.json and caches the login in token.json
  (`python uploader.py --auth`, once). In the cloud, set the secrets
  YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN instead.
* Upload: videos().insert() with #Shorts, category 20 (Gaming) and a credit line
  that names and links the original creator.
* Daily cap guard: never more than the daily limit (90 uploads in api mode, 12 clips
  in files mode) per Pacific-time day, and it stops for the rest of the day if
  Google says the quota is used up.

IMPORTANT: Google keeps every video uploaded by an app it has not audited
locked as PRIVATE, and the owner cannot unlock it. Until your Google Cloud
project passes the YouTube API audit, use the default files delivery instead.

Google's client libraries are imported lazily, so the rest of the bot works
even before they are installed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import config
from db import State
from permissions import Channel

if TYPE_CHECKING:  # keeps numpy out of the quick "any new uploads?" check
    from detector import Window

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RETRYABLE_STATUS = {500, 502, 503, 504}
QUOTA_REASONS = {
    "quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
    "rateLimitExceeded", "userRateLimitExceeded",
}
MAX_RETRIES = 6
PRIVACY_CHOICES = ("private", "unlisted", "public")


class AuthError(RuntimeError):
    """No usable login; run `python uploader.py --auth` (or set the cloud secrets)."""


class QuotaExceeded(RuntimeError):
    """Google says today's upload quota is gone."""


class DailyCapReached(RuntimeError):
    """Our own guard: today's budget is spent."""


class UploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    url: str
    privacy: str
    title: str = ""


# --------------------------------------------------------------------------
# Daily cap guard
# --------------------------------------------------------------------------
def daily_cap() -> int:
    cap = config.MAX_UPLOADS_PER_DAY if config.DELIVERY == "api" else config.MAX_CLIPS_PER_DAY
    return max(0, cap)


def cap_status(state: State) -> tuple[bool, str]:
    """(allowed, explanation). Checked before any download and again before delivery."""
    cap, used = daily_cap(), state.uploads_today()
    noun = "uploads" if config.DELIVERY == "api" else "clips"
    if config.DELIVERY == "api" and state.uploads_blocked_today():
        return False, "Google reported the upload quota is used up (resets at midnight Pacific time)"
    if used >= cap:
        return False, f"daily {noun} cap reached ({used}/{cap}; resets at midnight Pacific time)"
    return True, f"{used}/{cap} {noun} today"


# --------------------------------------------------------------------------
# Metadata (pure functions, no network)
# --------------------------------------------------------------------------
def clean_text(text: str | None) -> str:
    text = re.sub(r"[<>]", "", text or "")       # the API rejects < and > in titles/descriptions
    return re.sub(r"\s+", " ", text).strip()


def mmss(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_title(source_title: str | None, style: str = "plain", creator: str = "") -> str:
    """Title styles the bot can test: plain, creator (creator name first), caps (ALL CAPITALS)."""
    suffix = " #Shorts"
    base = clean_text(source_title) or "Gaming highlight"
    creator = clean_text(creator)
    if style == "creator" and creator:
        base = f"{creator}: {base}"
    elif style == "caps":
        base = base.upper()
    room = 100 - len(suffix)
    if len(base) > room:
        base = base[: room - 1].rstrip() + "\u2026"
    return base + suffix


def build_description(info: dict, channel: Channel, window: "Window") -> str:
    vid = info["id"]
    watch = f"https://www.youtube.com/watch?v={vid}"
    in_context = f"{watch}&t={int(window.start)}s"
    creator = clean_text(info.get("channel") or info.get("uploader") or channel.name) or "the original creator"
    title = clean_text(info.get("title"))
    channel_url = info.get("channel_url") or info.get("uploader_url")

    lines = [
        f"\U0001F3AE Highlight from \"{title}\" by {creator}",
        "",
        f"\u25b6 Watch the full video: {watch}",
        f"\u25b6 This moment in context: {in_context}",
    ]
    if channel_url:
        lines.append(f"\u25b6 Channel: {channel_url}")
    lines += [
        "",
        f"All footage belongs to {creator}. Posted with the creator's permission. This Short is an edited "
        f"excerpt ({mmss(window.start)}-{mmss(window.end)} of the original, trimmed, captioned and "
        "reformatted to vertical).",
        "",
        f"\U0001F514 Subscribe to {config.MY_CHANNEL_NAME} for more: {config.MY_CHANNEL_URL}",
        "",
        "#Shorts #gaming",
    ]
    return "\n".join(lines)[:4900]


def build_tags(info: dict, channel: Channel) -> list[str]:
    tags = ["Shorts", "gaming", "gaming highlights", config.MY_CHANNEL_NAME]
    creator = clean_text(info.get("channel") or channel.name).replace(",", "")
    if creator and len(creator) <= 30 and creator not in tags:
        tags.append(creator)
    return tags


def build_body(
    info: dict, channel: Channel, window: "Window", privacy: str | None = None, title_style: str = "plain"
) -> dict:
    privacy = privacy or config.PRIVACY_STATUS
    if privacy not in PRIVACY_CHOICES:
        raise ValueError(f"privacy must be one of {PRIVACY_CHOICES}, got {privacy!r}")
    return {
        "snippet": {
            "title": build_title(
                info.get("title"), title_style, info.get("channel") or info.get("uploader") or channel.name
            ),
            "description": build_description(info, channel, window),
            "tags": build_tags(info, channel),
            "categoryId": config.CATEGORY_ID,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
def _save_token(creds) -> None:
    config.TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(config.TOKEN_FILE, 0o600)
    except OSError:
        pass


def _env_login() -> tuple[str, str, str] | None:
    keys = (os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    client_id, client_secret, refresh = (k.strip() if k else "" for k in keys)
    return (client_id, client_secret, refresh) if client_id and client_secret and refresh else None


def get_credentials(interactive: bool = False, open_browser: bool = True, port: int = 0, force: bool = False):
    """Return valid OAuth credentials.

    Cloud: built from the YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN secrets.
    Computer: token.json (refreshed when needed).
    interactive=True (--auth): runs the browser login and writes token.json.
    force=True: ignore any saved login and log in again (switch Google account).
    """
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise AuthError("Google client libraries are missing - run: pip install -r requirements.txt") from exc

    env = None if force else _env_login()
    if env:
        client_id, client_secret, refresh = env
        creds = Credentials(
            token=None, refresh_token=refresh, token_uri=TOKEN_URI,
            client_id=client_id, client_secret=client_secret, scopes=SCOPES,
        )
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise AuthError(
                f"Google rejected the YT_REFRESH_TOKEN secret ({exc}). Make a new one - and make sure "
                "the OAuth consent screen is published ('In production'), because 'Testing' tokens expire after 7 days."
            ) from exc
        return creds

    creds = None
    if config.TOKEN_FILE.exists() and not force:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError as exc:
            if not interactive:
                raise AuthError(
                    f"the saved login was rejected ({exc}). If your OAuth app is still in 'Testing', "
                    "Google expires its tokens after 7 days - publish the app, then run: "
                    "python uploader.py --auth"
                ) from exc
            log.warning("saved login rejected (%s); starting a fresh login", exc)

    if not interactive:
        raise AuthError("no valid login - run once: python uploader.py --auth")
    if not config.CLIENT_SECRET_FILE.exists():
        raise AuthError(f"{config.CLIENT_SECRET_FILE} not found - see the README")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.CLIENT_SECRET_FILE), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=open_browser, prompt="consent", access_type="offline")
    _save_token(creds)
    return creds


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
def _error_reason(exc) -> str:
    try:
        payload = json.loads(exc.content.decode("utf-8", errors="replace"))
        return payload["error"]["errors"][0]["reason"]
    except Exception:
        return ""


def _insert(creds, path: Path, body: dict) -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    import httplib2

    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            status, reason = exc.resp.status, _error_reason(exc)
            if reason in QUOTA_REASONS:
                raise QuotaExceeded(f"{reason} (HTTP {status})") from exc
            if status == 401:
                raise AuthError("YouTube rejected the login (HTTP 401) - run: python uploader.py --auth") from exc
            if status in RETRYABLE_STATUS and retries < MAX_RETRIES:
                retries += 1
                time.sleep(min(60.0, 2 ** retries + random.random()))
                continue
            raise UploadError(f"HTTP {status} {reason}: {exc}") from exc
        except (httplib2.HttpLib2Error, socket.timeout, ConnectionError, OSError) as exc:
            if retries >= MAX_RETRIES:
                raise UploadError(f"network error during upload: {exc}") from exc
            retries += 1
            time.sleep(min(60.0, 2 ** retries + random.random()))
    return response


def upload_clip(
    state: State,
    clip_path: Path,
    info: dict,
    channel: Channel,
    window: "Window",
    privacy: str | None = None,
    creds=None,
    title_style: str = "plain",
) -> UploadResult:
    allowed, why = cap_status(state)
    if not allowed:
        raise DailyCapReached(why)
    privacy = privacy or config.PRIVACY_STATUS
    body = build_body(info, channel, window, privacy, title_style)
    creds = creds or get_credentials(interactive=False)
    try:
        response = _insert(creds, clip_path, body)
    except QuotaExceeded:
        state.block_uploads_today()
        raise

    new_id = response["id"]
    actual = (response.get("status") or {}).get("privacyStatus", privacy)
    if actual != privacy:
        log.warning(
            "requested %s but YouTube set the upload to %s. Apps Google has not audited are locked "
            "to private - see START-HERE.md, part C.", privacy, actual,
        )
    state.record_upload(info["id"], new_id, status="uploaded")
    return UploadResult(
        video_id=new_id, url=f"https://youtu.be/{new_id}", privacy=actual, title=body["snippet"]["title"]
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="YouTube login helper and upload status (api delivery mode)")
    parser.add_argument("--auth", action="store_true", help="one-time browser login; writes token.json")
    parser.add_argument("--no-browser", action="store_true", help="print the login URL instead of opening a browser")
    parser.add_argument("--port", type=int, default=0, help="local port for the login redirect (0 = any free port)")
    parser.add_argument("--status", action="store_true", help="show today's counter")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.status or not args.auth:
        state = State(config.DB_PATH)
        allowed, why = cap_status(state)
        print(f"today: {why} ({'OK' if allowed else 'BLOCKED'})")
        print(f"token.json: {'present' if config.TOKEN_FILE.exists() else 'missing - run --auth'}")
        if not args.auth:
            return 0
    try:
        get_credentials(interactive=True, open_browser=not args.no_browser, port=args.port, force=True)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Login saved to {config.TOKEN_FILE}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
