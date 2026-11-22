#!/usr/bin/env python3
"""main.py - the ClipForge Shorts bot.

    python main.py                          normal run (this is what the scheduler executes)
    python main.py --poll-only              only look for new uploads (fast; the cloud uses this first)
    python main.py --process-only           clip what is waiting, without polling again
    python main.py --seed-only              mark what exists now as "seen", then stop
    python main.py --dry-run                list what WOULD be picked up (changes nothing)
    python main.py --dry-run --video LINK   make one test Short from one video (nothing is delivered)
    python main.py --dry-run --local-file F test on a video file on this computer (no internet needed)
    python main.py --check-youtube          test whether YouTube lets THIS computer download videos
    python main.py --status                 counters and today's budget
    python main.py --report                 what the bot has learned from your Shorts' views
    python main.py --auth                   one-time YouTube login (only for api delivery)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
import db
import delivery
import feeds
import learner
import permissions
import uploader
import ytdlp
from db import State
from permissions import Channel

log = logging.getLogger("pipeline")

SKIP_HINTS = (
    "members-only", "join this channel", "private video", "video unavailable", "has been removed",
    "removed by the uploader", "not available in your country", "age-restricted", "confirm your age",
)
RETRY_HINTS = ("premieres in", "live event will begin", "this live event", "is processing", "will begin in")
BLOCK_HINTS = ("not a bot", "sign in to confirm")
TEST_VIDEO = "jNQXAC9IVRw"          # the first video ever uploaded to YouTube (19 s, always public)


class Skip(Exception):
    """Not an error: this video should not be clipped."""


class Retry(Exception):
    """Not ready yet (live, premiere, still processing): look again on a later run."""


class Blocked(Exception):
    """YouTube is refusing this computer (typical for cloud servers)."""


class AlreadyRunning(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------
def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass


def setup_cookies() -> None:
    """GitHub secrets arrive as text; yt-dlp wants a file."""
    if config.COOKIES_TEXT and not config.COOKIES_FILE:
        path = config.TMP_DIR / "cookies.txt"
        path.write_text(config.COOKIES_TEXT, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        config.COOKIES_FILE = str(path)


def set_output(name: str, value: str) -> None:
    """Pass a value to the next GitHub Actions step (ignored everywhere else)."""
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def annotate(level: str, title: str, message: str) -> None:
    """Put a short message at the top of a GitHub Actions run page (ignored everywhere else)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    def escape(text: str, is_property: bool = False) -> str:
        text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return text.replace(":", "%3A").replace(",", "%2C") if is_property else text

    print(f"::{level} title={escape(title, True)}::{escape(message)}")


def step_summary(markdown: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def _pid_alive(pid: int) -> bool:
    if os.name != "posix":
        return True          # signalling a pid is not safe on Windows; rely on lock age there
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale(path: Path, stale_after: float) -> bool:
    try:
        pid = int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and not _pid_alive(pid):
        return True                                   # the run that owned it died
    return time.time() - path.stat().st_mtime > stale_after


def heartbeat() -> None:
    """Refresh the lock's timestamp so a long, healthy run is never mistaken for a dead one."""
    try:
        os.utime(config.LOCK_FILE)
    except OSError:
        pass


@contextmanager
def single_instance(path: Path, stale_after: float = 45 * 60):
    """Keep two overlapping scheduled runs from fighting over the same files."""
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                stale = _lock_is_stale(path, stale_after)
            except FileNotFoundError:
                continue                              # released between our two calls: retry
            if not stale:
                raise AlreadyRunning(f"another run is in progress ({path.name}); exiting")
            log.warning("removing stale lock file %s", path.name)
            path.unlink(missing_ok=True)
    else:
        raise AlreadyRunning("could not acquire the lock; exiting")
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def load_channels(state: State) -> list[Channel]:
    """Read channels.txt. An empty or missing list is not an error: say what to do and carry on."""
    channels: list[Channel] = []
    problems: list[str] = []
    if config.CHANNELS_FILE.exists():
        channels, problems = permissions.load_channels(config.CHANNELS_FILE, cache=state)
    for problem in problems:
        log.warning("channels list: %s", problem)
    if problems and channels:
        annotate("warning", "Some channel links could not be read", "; ".join(problems[:3]))
    if not channels:
        message = (
            f"No channels to watch yet. Open {config.CHANNELS_FILE.name} and paste the link of each channel that "
            "said yes to clips, one per line. Until then the bot has nothing to do."
        )
        if problems:
            message += " Links that could not be read: " + "; ".join(problems[:3])
        log.warning(message)
        annotate("warning", "No channels yet", message)
        step_summary("### No channels yet\n" + message)
        return []
    log.info("%d approved channels loaded", len(channels))
    return channels


def classify(exc: Exception) -> str:
    text = str(exc).lower()
    if any(h in text for h in BLOCK_HINTS):
        return "blocked"
    if any(h in text for h in SKIP_HINTS):
        return "skip"
    if any(h in text for h in RETRY_HINTS):
        return "retry"
    return "fail"


def check_eligibility(info: dict) -> None:
    status = info.get("live_status")
    if status in ("is_live", "is_upcoming", "post_live"):
        raise Retry(f"not a finished video yet (live_status={status})")
    if info.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
        raise Skip(f"video availability is {info['availability']}")
    if (info.get("age_limit") or 0) >= 18:
        raise Skip("age-restricted video")
    duration = float(info.get("duration") or 0)
    if duration < config.MIN_SOURCE_SECONDS:
        raise Skip(f"only {duration:.0f}s long (already a Short, or too short to clip)")
    if duration > config.MAX_SOURCE_SECONDS:
        raise Skip(f"{duration / 3600:.1f} h long; too long for the audio fallback")


def local_info(path: Path) -> dict:
    import clipper

    meta = clipper.probe(path)
    return {
        "id": "localtest",
        "title": path.stem,
        "duration": clipper.duration_of(meta),
        "channel": "Local test",
        "channel_id": "UC" + "0" * 22,
        "live_status": "not_live",
        "availability": "public",
        "age_limit": 0,
    }


# --------------------------------------------------------------------------
# One video, start to finish
# --------------------------------------------------------------------------
def process_video(
    state: State | None,
    channel: Channel,
    ref: str,
    *,
    info: dict | None = None,
    local_media: Path | None = None,
    dry_run: bool,
    layout: str | None,
    privacy: str,
    plan: learner.Plan | None = None,
):
    import clipper          # heavy imports (numpy, captions) only when there is real work
    import detector

    plan = plan or learner.default_plan()
    layout = layout or plan.layout                       # an explicit --layout beats what the bot learned
    plan = dataclasses.replace(plan, layout=layout)

    vid = info["id"] if (local_media is not None and info) else ytdlp.video_id_from(ref)
    work = config.TMP_DIR / vid
    clip: Path | None = None
    try:
        if info is None:
            log.info("[%s] fetching video info", vid)
            info = ytdlp.fetch_info(ref)
        if local_media is None:
            check_eligibility(info)

        window = detector.find_best_window(info, ref, work, local_media=local_media, length=plan.seconds)
        log.info(
            "[%s] best moment %.1f-%.1fs of %.0fs (found by %s, peak at %.1fs)",
            vid, window.start, window.end, float(info.get("duration") or 0), window.method, window.peak,
        )

        out_dir = config.OUTPUT_DIR / "dry-run" if dry_run else config.OUTPUT_DIR
        clip = clipper.make_short(
            ref, window, out_dir / f"{vid}_short.mp4", work_dir=work, layout=layout, local_media=local_media,
            caption_words=plan.caption_words, caption_upper=plan.caption_upper,
        )

        if dry_run:
            body = uploader.build_body(info, channel, window, privacy, plan.title)
            print(json.dumps(body, indent=2, ensure_ascii=True))
            print(f"\nDry run OK. Your test Short is here: {clip}")
            return clip

        delivered = delivery.deliver(state, clip, info, channel, window, privacy, title_style=plan.title)
        learner.record_clip(state, info, channel, window, plan, delivered)
        return delivered
    finally:
        shutil.rmtree(work, ignore_errors=True)                       # raw temp files never linger
        if clip is not None and not dry_run and not config.KEEP_FINAL_CLIPS:
            clip.unlink(missing_ok=True)


def attempt(state: State, channel: Channel, vid: str, args, info: dict | None = None) -> str:
    """Run one video and record the outcome.

    Returns delivered | skipped | retry | failed | stop | auth | blocked.
    """
    try:
        plan = learner.choose_plan(state)
        result = process_video(
            state, channel, vid, info=info, dry_run=False, layout=args.layout, privacy=args.privacy, plan=plan
        )
        learner.after_outcome(state, channel, result.kind)
        if result.kind == "ready":
            log.info("[%s] READY -> %s", vid, result.where)
        else:
            log.info("[%s] uploaded -> %s", vid, result.where)
        return "delivered"
    except Skip as exc:
        state.mark(vid, "skipped", str(exc))
        log.info("[%s] skipped: %s", vid, exc)
        return "skipped"
    except Retry as exc:
        state.touch(vid, str(exc))
        log.info("[%s] will look again later: %s", vid, exc)
        return "retry"
    except (uploader.DailyCapReached, uploader.QuotaExceeded) as exc:
        log.warning("[%s] stopping for today: %s", vid, exc)
        return "stop"
    except uploader.AuthError as exc:
        log.error("%s", exc)
        return "auth"
    except Exception as exc:  # noqa: BLE001 - one bad video must not kill the whole run
        kind = classify(exc)
        if kind == "blocked":
            state.touch(vid, "YouTube blocked the download; will try again")
            log.error(
                "YouTube is refusing to let this computer download videos (%s). Cloud servers are often "
                "blocked. Run the bot on your own PC instead, or see START-HERE.md, part B.", exc,
            )
            annotate("error", "YouTube is blocking this computer",
                     "YouTube refuses downloads from this computer. Cloud servers are often blocked. Run the bot on your own PC (START-HERE.md, Part A).")
            return "blocked"
        if kind == "skip":
            state.mark(vid, "skipped", str(exc))
            log.info("[%s] skipped: %s", vid, exc)
            return "skipped"
        if kind == "retry":
            state.touch(vid, str(exc))
            log.info("[%s] will look again later: %s", vid, exc)
            return "retry"
        attempts = state.attempts(vid) + 1
        status = "failed" if attempts >= config.MAX_ATTEMPTS else "new"
        state.mark(vid, status, f"attempt {attempts}: {exc}", bump_attempt=True)
        log.error("[%s] attempt %d/%d failed: %s", vid, attempts, config.MAX_ATTEMPTS, exc)
        if status == "failed":
            learner.after_outcome(state, channel, "failed")
        return "failed"


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def poll_and_register(state: State, channels: list[Channel], *, seed_only: bool, dry_run: bool) -> None:
    results = feeds.poll_all([c.id for c in channels], config.FEED_WORKERS)
    seeded = registered = failed = 0
    for ch in channels:
        res = results[ch.id]
        if isinstance(res, Exception):
            failed += 1
            log.warning("feed failed for %s: %s", ch.name, res)
            continue
        if not state.channel_seeded(ch.id):
            if dry_run:
                log.info("[dry-run] would seed %s: %d existing videos marked as seen", ch.name, len(res))
            else:
                seeded += state.seed_channel(ch.id, res)
                log.info("seeded %s: %d existing videos marked as seen", ch.name, len(res))
            continue
        if seed_only:
            continue
        if dry_run:
            for entry in res:
                if not state.known(entry.video_id):
                    log.info("[dry-run] new upload from %s: %s (%s)", ch.name, entry.title, entry.video_id)
            continue
        fresh = state.register_new(ch.id, res, config.MAX_VIDEO_AGE_HOURS)
        registered += len(fresh)
        for entry in res:
            if entry.video_id in fresh:
                log.info("new upload from %s: %s (%s)", ch.name, entry.title, entry.video_id)
    if not seed_only and not dry_run:
        waiting = {r["video_id"]: r["views_seen"] for r in state.waiting()}
        for res in results.values():
            if isinstance(res, Exception):
                continue
            for entry in res:
                if entry.video_id in waiting and entry.views and entry.views != waiting[entry.video_id]:
                    state.update_views(entry.video_id, entry.views)
    log.info(
        "checked %d channels (%d could not be read); %d old videos marked as seen, %d new uploads queued",
        len(channels), failed, seeded, registered,
    )


def process_pending(state: State, channels: list[Channel], args) -> Counter:
    by_id = {c.id: c for c in channels}
    stats: Counter = Counter()
    for row in learner.rank_pending(state, state.waiting())[: args.max_videos]:
        heartbeat()
        vid = row["video_id"]
        allowed, why = uploader.cap_status(state)
        if not allowed:
            log.warning("not processing further videos: %s", why)
            break
        channel = by_id.get(row["channel_id"])
        if channel is None:
            state.mark(vid, "skipped", "channel no longer in your list")
            continue
        age = db.age_hours(row["published"])
        if age is not None and age > config.MAX_VIDEO_AGE_HOURS:
            state.mark(vid, "skipped", f"stale ({age:.0f}h old)")
            stats["skipped"] += 1
            continue
        outcome = attempt(state, channel, vid, args)
        stats[outcome] += 1
        if outcome in ("stop", "auth", "blocked"):
            break
    return stats


def maybe_update_ytdlp(state: State) -> None:
    """Keep yt-dlp current without any help: YouTube breaks old versions every few weeks."""
    if not config.AUTO_UPDATE or os.environ.get("GITHUB_ACTIONS"):
        return                                           # the cloud installs the newest version every run
    due = state.get_meta("ytdlp_next_update")
    if due:
        age = db.age_hours(due)
        if age is not None and age < 0:
            return                                       # not due yet
    log.info("updating yt-dlp (about once a week; keeps downloads working)")
    ok = False
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", "-q", "yt-dlp[default]"],
            capture_output=True, text=True, errors="replace", timeout=900,
        )
        ok = proc.returncode == 0
        if not ok:
            log.warning("yt-dlp update failed (will retry later): %s", (proc.stderr or "").strip()[-200:])
    except Exception as exc:  # noqa: BLE001
        log.warning("yt-dlp update failed (will retry later): %s", exc)
    wait = timedelta(days=7) if ok else timedelta(hours=6)
    state.set_meta("ytdlp_next_update", (datetime.now(timezone.utc) + wait).replace(microsecond=0).isoformat())


def run_cycle(args, state: State, channels: list[Channel], *, poll: bool, process: bool) -> int:
    try:
        measured = learner.track_results(state, channels)       # how did our past Shorts do? (one tiny request)
        if measured.get("matched"):
            log.info("measured %d Shorts on your channel (%d with a 24-hour score)", measured["matched"], measured["scored"])
    except Exception as exc:  # noqa: BLE001 - learning must never stop the bot from working
        log.info("could not update results: %s", exc)
    if poll:
        poll_and_register(state, channels, seed_only=args.seed_only, dry_run=False)
        if args.seed_only:
            return 0
    if process:
        maybe_update_ytdlp(state)
        removed = delivery.cleanup_ready()
        if removed:
            log.info("tidied away %d old finished files", removed)
        stats = process_pending(state, channels, args)
        _, why = uploader.cap_status(state)
        log.info("run complete: %s | %s", dict(stats) or "nothing to process", why)
        if stats.get("blocked"):
            step_summary("### YouTube is blocking this computer\nSee START-HERE.md, part B.")
    else:
        waiting = len(state.pending(1))
        allowed, why = uploader.cap_status(state)
        has_work = waiting > 0 and allowed
        set_output("has_work", "true" if has_work else "false")
        log.info("%s waiting to be clipped (%s)", "videos are" if waiting else "nothing is", why)
    return 0


def dry_run_one(args, channel: Channel, ref: str, *, info: dict, local_media: Path | None = None) -> int:
    """Run one video through detect + clip + captions, print the payload, never deliver."""
    try:
        process_video(
            None, channel, ref, info=info, local_media=local_media,
            dry_run=True, layout=args.layout, privacy=args.privacy,
        )
        return 0
    except Skip as exc:
        print(f"Dry run: a real run would skip this video - {exc}")
        return 0
    except Retry as exc:
        print(f"Dry run: not ready yet - {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            log.exception("dry run failed")
        print(f"Dry run FAILED: {exc}", file=sys.stderr)
        return 1


def run_single(args, state: State | None, channels: list[Channel]) -> int:
    if args.local_file:
        if not args.dry_run:
            print("--local-file only works together with --dry-run (it never delivers anything)", file=sys.stderr)
            return 2
        path = Path(args.local_file).expanduser().resolve()
        info = local_info(path)
        channel = Channel(id=info["channel_id"], name="Local test")
        return dry_run_one(args, channel, str(path), info=info, local_media=path)

    vid = ytdlp.video_id_from(args.video)
    info = ytdlp.fetch_info(args.video)
    by_id = {c.id: c for c in channels}
    channel = by_id.get(info.get("channel_id")) or Channel(
        id=info.get("channel_id") or "unknown", name=info.get("channel") or info.get("uploader") or ""
    )
    if args.dry_run:
        return dry_run_one(args, channel, vid, info=info)
    with single_instance(config.LOCK_FILE):
        row = state.conn.execute("SELECT status FROM videos WHERE video_id = ?", (vid,)).fetchone()
        if row and row["status"] in ("uploaded", "ready"):
            print(f"{vid} was already done; nothing to do")
            return 0
        state.ensure_video(vid, channel.id, info.get("title", ""))
        state.mark(vid, "new", "requested with --video")
        outcome = attempt(state, channel, vid, args, info=info)
    return 0 if outcome in ("delivered", "skipped") else 1


def check_youtube() -> int:
    """Plain-English test of everything the bot needs from the computer it runs on.

    Lines marked PROBLEM must be fixed. Lines marked NOTE are nice to have and never fail the check.
    """
    import tempfile

    must: list[tuple[bool, str]] = []
    notes: list[tuple[bool, str]] = []

    must.append((bool(shutil.which("ffmpeg")), "ffmpeg is installed"))
    js = [r for r in ("deno", "node", "bun", "qjs") if shutil.which(r)]
    must.append((bool(js), f"a JavaScript runtime for yt-dlp is installed ({', '.join(js) or 'none found - install Deno'})"))

    try:
        import captions

        cap_ok = captions.filter_available() and captions.whisper_available()
    except Exception:  # noqa: BLE001
        cap_ok = False
    notes.append((cap_ok, "automatic captions are available (ffmpeg libass + faster-whisper). Without them Shorts have no captions"))

    try:
        import resolve

        refs = permissions.parse_channel_lines(config.CHANNELS_FILE.read_text(encoding="utf-8-sig")) if config.CHANNELS_FILE.exists() else []
        handles = [r for r in refs if not resolve.channel_id_in(r)][:3]
        if handles:
            readable = 0
            for ref in handles:
                try:
                    resolve.resolve_channel(ref)
                    readable += 1
                except Exception:  # noqa: BLE001
                    pass
            notes.append((
                readable == len(handles),
                f"channel links in your list can be read from here ({readable} of {len(handles)} tested). "
                "If not, paste the 24-character UC... channel id instead of the link",
            ))
    except Exception:  # noqa: BLE001
        pass

    blocked = False
    try:
        ytdlp.fetch_info(TEST_VIDEO, timeout=120)
        must.append((True, "YouTube lets this computer look up videos"))
        with tempfile.TemporaryDirectory() as tmp:
            ytdlp.run(
                ["-f", "b[height<=360]/worst", "--download-sections", "*0-4", "--quiet", "--no-progress",
                 "-o", str(Path(tmp) / "check.%(ext)s"), ytdlp.video_url(TEST_VIDEO)],
                timeout=300,
            )
            got = [p for p in Path(tmp).iterdir() if p.is_file() and p.stat().st_size > 1000]
        must.append((bool(got), "YouTube lets this computer download video"))
    except Exception as exc:  # noqa: BLE001
        blocked = classify(exc) == "blocked"
        if blocked:
            must.append((False, "YouTube is BLOCKING this computer (it does this to many cloud servers)"))
        else:
            must.append((False, f"could not talk to YouTube: {str(exc)[:300]}"))

    ok = all(good for good, _ in must)
    if ok:
        verdict = "RESULT: this computer CAN run the bot."
    elif blocked:
        verdict = (
            "RESULT: YouTube is blocking this computer. Nothing is wrong with your setup: YouTube refuses "
            "downloads from many cloud servers. Run the bot on your own PC instead (START-HERE.md, Part A)."
        )
    else:
        verdict = "RESULT: not ready. Fix the lines marked PROBLEM first."

    for good, text in must:
        print(f"[{'OK' if good else 'PROBLEM'}] {text}")
    for good, text in notes:
        print(f"[{'OK' if good else 'NOTE'}] {text}")
    print("\n" + verdict)

    annotate("notice" if ok else "error", "ClipForge check", verdict)
    md = ["### ClipForge check", ""]
    md += [f"- {'✅' if good else '❌'} {text}" for good, text in must]
    md += [f"- {'✅' if good else 'ℹ️'} {text}" for good, text in notes]
    md += ["", f"**{verdict}**"]
    step_summary("\n".join(md))
    return 0 if ok else 1


def show_report() -> int:
    state = State(config.DB_PATH)
    channels: list[Channel] = []
    if config.CHANNELS_FILE.exists():
        channels, _ = permissions.load_channels(config.CHANNELS_FILE, cache=state)
    learner.track_results(state, channels, force=True)
    text = learner.report(state, channels)
    config.REPORT_FILE.write_text(text, encoding="utf-8")
    print(text)
    print(f"(saved as {config.REPORT_FILE})")
    step_summary(text)
    state.close()
    return 0


def show_status() -> int:
    state = State(config.DB_PATH)
    print("videos by status:", state.counts() or "nothing recorded yet")
    allowed, why = uploader.cap_status(state)
    print(f"delivery mode: {config.DELIVERY}")
    print(learner.summary_line(state))
    print(f"today: {why} ({'OK' if allowed else 'LIMIT REACHED'})")
    if config.DELIVERY == "files":
        ready = sorted(config.READY_DIR.glob("*.mp4")) if config.READY_DIR.exists() else []
        print(f"finished Shorts waiting in {config.READY_DIR}: {len(ready)}")
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ClipForge Shorts bot.")
    p.add_argument("--dry-run", action="store_true", help="never deliver or change what the bot remembers; see the module docstring")
    p.add_argument("--video", metavar="LINK_OR_ID", help="process one specific video")
    p.add_argument("--local-file", metavar="PATH", help="dry-run on a local video file (no internet)")
    p.add_argument("--poll-only", action="store_true", help="only look for new uploads")
    p.add_argument("--process-only", action="store_true", help="only clip what is waiting")
    p.add_argument("--seed-only", action="store_true", help="mark current feed contents as seen, then exit")
    p.add_argument("--check-youtube", action="store_true", help="test whether this computer can run the bot")
    p.add_argument("--status", action="store_true", help="show counters and today's budget")
    p.add_argument("--report", action="store_true", help="what the bot has learned so far (also saved as LEARNING-REPORT.md)")
    p.add_argument("--auth", action="store_true", help="one-time YouTube login (api delivery only)")
    p.add_argument("--no-browser", action="store_true", help="with --auth: print the URL instead of opening a browser")
    p.add_argument("--port", type=int, default=0, help="with --auth: fixed local port (use 8080 over SSH)")
    p.add_argument("--max-videos", type=int, default=config.MAX_VIDEOS_PER_RUN, help="videos to process per run")
    p.add_argument("--layout", choices=("blur", "crop"), default=None, help="force a layout (otherwise the bot learns it)")
    p.add_argument("--privacy", default=config.PRIVACY_STATUS, help="api mode only: private | unlisted | public")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.privacy not in uploader.PRIVACY_CHOICES:
        p.error(f"--privacy / YT_PRIVACY must be one of {', '.join(uploader.PRIVACY_CHOICES)}")
    if config.LAYOUT not in ("blur", "crop"):
        p.error("YT_LAYOUT must be 'blur' or 'crop'")
    if config.DELIVERY not in ("files", "api"):
        p.error("YT_DELIVERY must be 'files' or 'api'")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")          # channel names in other alphabets must not crash old consoles
    except Exception:  # noqa: BLE001
        pass
    setup_logging(args.verbose)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    setup_cookies()

    if args.auth:
        return uploader.main(
            ["--auth"] + (["--no-browser"] if args.no_browser else []) + ["--port", str(args.port)]
        )
    if args.status:
        return show_status()
    if args.report:
        return show_report()
    if args.check_youtube:
        return check_youtube()

    try:
        if args.local_file:
            return run_single(args, None, [])

        state = State(config.DB_PATH)
        if args.video:
            channels, _ = (permissions.load_channels(config.CHANNELS_FILE, cache=state)
                           if config.CHANNELS_FILE.exists() else ([], []))
            code = run_single(args, state, channels)
            state.close()
            return code

        channels = load_channels(state)
        if not channels:
            set_output("has_work", "false")
            state.close()
            return 0
        if args.dry_run:
            poll_and_register(state, channels, seed_only=False, dry_run=True)
            state.close()
            return 0

        poll = not args.process_only
        process = not (args.poll_only or args.seed_only)
        with single_instance(config.LOCK_FILE):
            code = run_cycle(args, state, channels, poll=poll, process=process)
        state.close()
        return code
    except AlreadyRunning as exc:
        log.info("%s", exc)
        set_output("has_work", "false")
        return 0
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
