"""learner.py - the bot's memory of what worked, and the decisions it takes from it.

How it learns, in plain terms:

1. Every Short the bot makes is stored with the choices behind it: layout, clip length,
   caption style, title style, which source channel it came from.
2. The bot reads the public feed of YOUR channel (free, no login) and links each posted
   Short back to its clip (through the source link in the description, or the title).
   It records the views after 6, 24 and 72 hours.
3. The score of a Short is log10(1 + views after 24 hours). Logs keep one lucky viral
   Short from fooling the bot, and keep a 50-view Short and a 500-view Short comparable.
4. When the bot must choose something, it uses Thompson sampling: for every option it
   draws a plausible score from what it knows so far and takes the best draw. Options
   with little data get wide guesses, so new things still get tried (exploring); options
   with lots of good data win almost every time (exploiting). Nothing is hard-coded to
   "always do X".
5. Mistakes: a source channel whose clips keep failing is paused for a week; a channel
   whose Shorts get far fewer views than your average is paused for two weeks. Every
   decision is written to a diary you can read (`python main.py --report`).

Honest limits: YouTube decides reach, views are noisy, and a new channel needs about 30
measured Shorts before patterns are reliable. This improves the odds; it cannot guarantee views.
"""
from __future__ import annotations

import logging
import math
import random
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
import db
import feeds
import resolve

log = logging.getLogger(__name__)

LAYOUTS = ("blur", "crop")
LENGTHS = ("48", "53", "58")                                    # seconds; Shorts must stay under 59
CAPTION_STYLES = {"upper3": (3, True), "upper2": (2, True), "lower3": (3, False)}   # (words per line, UPPERCASE)
TITLE_STYLES = ("plain", "creator", "caps")

PRIOR_MEAN = 1.8          # log10(1 + ~60 views): a typical brand-new channel
PRIOR_SD = 0.7
PRIOR_STRENGTH = 2        # the prior counts as this many pretend observations
TRACTION_WEIGHT = 0.25    # how much a source video's own momentum counts when ranking
PAUSE_AFTER_FAILURES = 4
PAUSE_DAYS_FAILING = 7
PAUSE_DAYS_WEAK = 14
WEAK_MIN_CLIPS = 6
WEAK_GAP = 0.6            # 0.6 in log10 = roughly 4x fewer views than your average
TRACK_EVERY_HOURS = 0.5
MATURE_AT = 30            # measured Shorts before the report stops saying "still exploring"

_SOURCE_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([0-9A-Za-z_-]{11})")
_ARM_COLUMNS = {
    "layout": "layout", "length": "length_arm", "caption": "caption_arm",
    "title": "title_arm", "channel": "source_channel_id",
}


# --------------------------------------------------------------------------
# What the bot decides for one Short
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    layout: str = "blur"
    length: str = "55"
    caption: str = "upper3"
    title: str = "plain"
    words: int | None = None          # explicit caption settings (only when you set them yourself)
    upper: bool | None = None

    @property
    def seconds(self) -> float:
        return float(self.length)

    @property
    def caption_words(self) -> int:
        return self.words if self.words is not None else CAPTION_STYLES.get(self.caption, (3, True))[0]

    @property
    def caption_upper(self) -> bool:
        return self.upper if self.upper is not None else CAPTION_STYLES.get(self.caption, (3, True))[1]


def default_plan() -> Plan:
    """What you configured, with no learning."""
    style = next(
        (name for name, value in CAPTION_STYLES.items() if value == (config.CAPTION_WORDS, config.CAPTION_UPPER)),
        "custom",
    )
    return Plan(
        layout=config.LAYOUT,
        length=str(int(round(config.CLIP_TARGET_SECONDS))),
        caption=style,
        title="plain",
        words=config.CAPTION_WORDS,
        upper=config.CAPTION_UPPER,
    )


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
def arm_stats(state, dimension: str) -> dict[str, tuple[int, float]]:
    """{option: (number of measured Shorts, average score)} for one kind of choice."""
    column = _ARM_COLUMNS[dimension]
    rows = state.conn.execute(
        f"SELECT {column} AS arm, COUNT(reward) AS n, AVG(reward) AS mean "
        f"FROM clips WHERE reward IS NOT NULL GROUP BY {column}"
    ).fetchall()
    return {r["arm"]: (int(r["n"]), float(r["mean"])) for r in rows if r["arm"] is not None and r["n"]}


def global_stats(state) -> tuple[int, float, float]:
    """(measured Shorts, average score, spread). Falls back to a sensible prior while data is thin."""
    values = [float(r["reward"]) for r in state.conn.execute("SELECT reward FROM clips WHERE reward IS NOT NULL")]
    if len(values) < 3:
        return len(values), PRIOR_MEAN, PRIOR_SD
    return len(values), statistics.fmean(values), max(0.3, statistics.pstdev(values))


def _sample(n: int, mean: float, prior_mean: float, prior_sd: float, rng: random.Random) -> float:
    weight = n + PRIOR_STRENGTH
    posterior_mean = (mean * n + prior_mean * PRIOR_STRENGTH) / weight
    return rng.gauss(posterior_mean, prior_sd / math.sqrt(weight))


def pick_arm(arms, stats, prior_mean: float, prior_sd: float, rng: random.Random) -> str:
    """Thompson sampling: draw a plausible score for each option, take the best draw."""
    best, best_score = None, -math.inf
    for arm in arms:
        n, mean = stats.get(arm, (0, prior_mean))
        score = _sample(n, mean, prior_mean, prior_sd, rng)
        if score > best_score:
            best, best_score = arm, score
    return best


def views_from_score(score: float) -> int:
    return max(0, int(round(10 ** score - 1)))


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------
def choose_plan(state, rng: random.Random | None = None) -> Plan:
    """Pick layout, length, caption style and title style for the next Short."""
    plan = default_plan()
    if not config.LEARNING or state is None:
        return plan
    rng = rng or random.Random()
    _, mean, sd = global_stats(state)
    changes: dict = {}
    if "layout" not in config.FIXED:
        changes["layout"] = pick_arm(LAYOUTS, arm_stats(state, "layout"), mean, sd, rng)
    if "length" not in config.FIXED:
        changes["length"] = pick_arm(LENGTHS, arm_stats(state, "length"), mean, sd, rng)
    if "caption" not in config.FIXED:
        changes["caption"] = pick_arm(tuple(CAPTION_STYLES), arm_stats(state, "caption"), mean, sd, rng)
        changes["words"], changes["upper"] = None, None
    changes["title"] = pick_arm(TITLE_STYLES, arm_stats(state, "title"), mean, sd, rng)
    return Plan(**{**plan.__dict__, **changes})


def traction(row) -> float:
    """How fast the source video is gaining views: log10(1 + views per hour)."""
    views = row["views_seen"] or 0
    age = db.age_hours(row["published"])
    age = 0.25 if age is None else max(0.25, age)
    return math.log10(1 + views / age)


def rank_pending(state, rows, rng: random.Random | None = None) -> list:
    """Order the waiting videos by expected payoff: proven channels and rising videos first."""
    if not config.LEARNING:
        return list(rows)
    rng = rng or random.Random()
    _, mean, sd = global_stats(state)
    channel_stats = arm_stats(state, "channel")
    scored = []
    for row in rows:
        if is_paused(state, row["channel_id"]):
            continue
        n, channel_mean = channel_stats.get(row["channel_id"], (0, mean))
        value = _sample(n, channel_mean, mean, sd, rng) + TRACTION_WEIGHT * traction(row)
        scored.append((value, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored]


# --------------------------------------------------------------------------
# Memory: what we made, what happened
# --------------------------------------------------------------------------
def record_clip(state, info: dict, channel, window, plan: Plan, delivered) -> None:
    """Store the choices behind a finished Short so its results can be matched to them later."""
    duration = float(info.get("duration") or 0)
    stamp = info.get("timestamp")
    source_age = max(0.0, (time.time() - float(stamp)) / 3600.0) if stamp else None
    with state.conn:
        state.conn.execute(
            "INSERT OR REPLACE INTO clips(source_video_id, source_channel_id, source_title, source_views,"
            " source_age_hours, created_at, delivered_kind, my_video_id, my_title, layout, length_arm,"
            " caption_arm, title_arm, method, start_frac) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                info["id"], channel.id, info.get("title"), info.get("view_count"), source_age, db.utcnow(),
                delivered.kind, delivered.video_id or None, delivered.title or None,
                plan.layout, plan.length, plan.caption, plan.title, window.method,
                (window.start / duration) if duration else None,
            ),
        )


def add_lesson(state, kind: str, detail: str) -> None:
    with state.conn:
        state.conn.execute("INSERT INTO lessons(ts, kind, detail) VALUES (?, ?, ?)", (db.utcnow(), kind, detail))
    log.info("learned: %s", detail)


def recent_lessons(state, limit: int = 10) -> list:
    return state.conn.execute("SELECT ts, kind, detail FROM lessons ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def my_channel_id(state) -> str | None:
    if config.MY_CHANNEL_ID:
        return config.MY_CHANNEL_ID
    cached = state.get_meta("my_channel_id")
    if cached:
        return cached
    try:
        channel_id, _ = resolve.resolve_channel(config.MY_CHANNEL_URL)
    except Exception as exc:  # noqa: BLE001
        log.info("could not find your channel id (%s); set YT_MY_CHANNEL_ID", exc)
        return None
    state.set_meta("my_channel_id", channel_id)
    return channel_id


def _find_clip(state, entry):
    row = state.conn.execute("SELECT * FROM clips WHERE my_video_id = ?", (entry.video_id,)).fetchone()
    if row:
        return row
    for match in _SOURCE_RE.finditer(entry.description or ""):          # the source link we put in the description
        row = state.conn.execute(
            "SELECT * FROM clips WHERE source_video_id = ? AND my_video_id IS NULL", (match.group(1),)
        ).fetchone()
        if row:
            return row
    if entry.title:                                                      # fallback: the title we generated
        return state.conn.execute(
            "SELECT * FROM clips WHERE my_title = ? AND my_video_id IS NULL", (entry.title,)
        ).fetchone()
    return None


def ingest_feed(state, entries, now: datetime | None = None) -> dict:
    """Link posted Shorts to our clips, store view snapshots at 6/24/72 h, and compute scores."""
    now = now or datetime.now(timezone.utc)
    matched = scored = 0
    for entry in entries:
        clip = _find_clip(state, entry)
        posted = db._parse_time(entry.published)
        if clip is None or posted is None:
            continue
        age = (now - posted).total_seconds() / 3600.0
        values: dict = {
            "my_video_id": entry.video_id, "posted_at": entry.published,
            "last_views": entry.views, "last_likes": entry.likes,
            "last_checked": now.replace(microsecond=0).isoformat(),
        }
        if not clip["my_title"]:
            values["my_title"] = entry.title
        for hours, column in ((6, "views_6h"), (24, "views_24h"), (72, "views_72h")):
            if age >= hours and clip[column] is None:
                values[column] = entry.views
        views_24h = values.get("views_24h", clip["views_24h"])
        if views_24h is None and 24 <= age <= 96:          # our first look came late: best available number
            views_24h = entry.views
        if views_24h is not None:
            values["reward"] = math.log10(1 + max(0, views_24h))
            scored += 1
        assignments = ", ".join(f"{column} = ?" for column in values)
        state.conn.execute(
            f"UPDATE clips SET {assignments} WHERE source_video_id = ?",
            (*values.values(), clip["source_video_id"]),
        )
        matched += 1
    state.conn.commit()
    return {"matched": matched, "scored": scored}


def track_results(state, channels=None, force: bool = False, fetch=None) -> dict:
    """Read our own channel's public feed (at most every 30 minutes) and update what we know."""
    if not config.LEARNING:
        return {}
    last = state.get_meta("last_track")
    if last and not force:
        waited = db.age_hours(last)
        if waited is not None and waited < TRACK_EVERY_HOURS:
            return {}
    channel_id = my_channel_id(state)
    if not channel_id:
        return {}
    try:
        entries = (fetch or (lambda cid: feeds.fetch_feed(cid, retries=0)))(channel_id)
    except Exception as exc:  # noqa: BLE001
        log.info("could not read your channel feed right now: %s", exc)
        return {}
    result = ingest_feed(state, entries)
    state.set_meta("last_track", db.utcnow())
    if channels:
        review_channels(state, channels)
    return result


# --------------------------------------------------------------------------
# Learning from mistakes and weak sources
# --------------------------------------------------------------------------
def _pause_key(channel_id: str) -> str:
    return f"pause:{channel_id}"


def is_paused(state, channel_id: str) -> bool:
    until = state.get_meta(_pause_key(channel_id))
    if not until:
        return False
    remaining = db.age_hours(until)                 # negative while the pause is still in the future
    if remaining is not None and remaining < 0:
        return True
    state.set_meta(_pause_key(channel_id), "")
    add_lesson(state, "resumed", f"giving channel {channel_id} another chance (its pause is over)")
    return False


def pause_channel(state, channel, days: int, reason: str) -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
    state.set_meta(_pause_key(channel.id), until)
    add_lesson(state, "paused", f"paused {channel.name} for {days} days: {reason}")


def after_outcome(state, channel, status: str) -> None:
    """Called when a video reaches a final result. Several failures in a row pause the channel."""
    if not config.LEARNING or status not in ("ready", "uploaded", "failed"):
        return
    rows = state.conn.execute(
        "SELECT status FROM videos WHERE channel_id = ? AND status IN ('ready','uploaded','failed')"
        " ORDER BY updated_at DESC, rowid DESC LIMIT ?",
        (channel.id, PAUSE_AFTER_FAILURES),
    ).fetchall()
    if len(rows) == PAUSE_AFTER_FAILURES and all(r["status"] == "failed" for r in rows) and not is_paused(state, channel.id):
        pause_channel(state, channel, PAUSE_DAYS_FAILING, f"its last {PAUSE_AFTER_FAILURES} videos could not be turned into Shorts")


def review_channels(state, channels) -> None:
    """Pause channels whose Shorts consistently get far fewer views than your average."""
    n, mean, _ = global_stats(state)
    if n < 10:
        return
    stats = arm_stats(state, "channel")
    for channel in channels:
        count, channel_mean = stats.get(channel.id, (0, mean))
        if count >= WEAK_MIN_CLIPS and channel_mean < mean - WEAK_GAP and not is_paused(state, channel.id):
            pause_channel(
                state, channel, PAUSE_DAYS_WEAK,
                f"its {count} Shorts average about {views_from_score(channel_mean)} views against "
                f"{views_from_score(mean)} for the rest",
            )


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def best_hours(state, top: int = 2, min_total: int = 12) -> list[int] | None:
    """Hours of the day (UTC) when your Shorts got the most views, once there is enough data."""
    rows = state.conn.execute(
        "SELECT posted_at, reward FROM clips WHERE reward IS NOT NULL AND posted_at IS NOT NULL"
    ).fetchall()
    if len(rows) < min_total:
        return None
    _, mean, _ = global_stats(state)
    buckets: dict[int, list[float]] = {}
    for row in rows:
        posted = db._parse_time(row["posted_at"])
        if posted:
            buckets.setdefault(posted.astimezone(timezone.utc).hour, []).append(float(row["reward"]))
    scored = [
        (((sum(values) + mean * PRIOR_STRENGTH) / (len(values) + PRIOR_STRENGTH)), hour)
        for hour, values in buckets.items() if len(values) >= 2
    ]
    return [hour for _, hour in sorted(scored, reverse=True)[:top]] or None


def post_time_hint(state) -> str:
    hours = best_hours(state)
    if not hours:
        return ""
    return "BEST TIME TO POST so far (UTC): " + " or ".join(f"{h:02d}:00" for h in sorted(hours)) + "\n"


def summary_line(state) -> str:
    n, mean, _ = global_stats(state)
    made = state.conn.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"]
    if not made:
        return "learning: no Shorts made yet"
    if not n:
        return f"learning: {made} Shorts made, none measured yet (post them with the given title and description)"
    return f"learning: {n} of {made} Shorts measured, typical {views_from_score(mean)} views at 24 hours"


def _table(state, dimension: str, arms, label) -> list[str]:
    stats = arm_stats(state, dimension)
    lines = ["| option | measured Shorts | typical views at 24 h |", "|---|---|---|"]
    best = max(stats, key=lambda a: stats[a][1]) if stats else None
    for arm in arms:
        n, mean = stats.get(arm, (0, 0.0))
        shown = f"{views_from_score(mean)}" if n else "-"
        star = " **(best so far)**" if arm == best and n >= 3 else ""
        lines.append(f"| {label(arm)}{star} | {n} | {shown} |")
    return lines


def report(state, channels=None) -> str:
    n, mean, _ = global_stats(state)
    made = state.conn.execute("SELECT COUNT(*) AS n FROM clips").fetchone()["n"]
    waiting = state.conn.execute(
        "SELECT COUNT(*) AS n FROM clips WHERE my_video_id IS NULL AND created_at < ?",
        ((datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat(),),
    ).fetchone()["n"]
    names = {c.id: c.name for c in (channels or [])}
    lines = [f"# ClipForge learning report - {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    lines += ["## Where things stand", ""]
    lines.append(f"- Shorts made so far: **{made}**")
    lines.append(f"- Shorts measured (posted and at least 24 hours old): **{n}**")
    if n:
        lines.append(f"- Typical views at 24 hours: **about {views_from_score(mean)}**")
    if n < MATURE_AT:
        lines.append(
            f"- Status: **still exploring.** The bot tries different options on purpose until it has about "
            f"{MATURE_AT} measured Shorts. Until then differences are mostly luck."
        )
    else:
        lines.append("- Status: **exploiting what works**, with a little exploring on the side.")
    if waiting:
        lines.append(
            f"- {waiting} Shorts could not be matched to a video on your channel after 24 hours. "
            "Post them with the exact title and description from the .txt file so the bot can measure them."
        )
    lines += ["", "## What works best so far", "", "**Look of the Short**", ""]
    lines += _table(state, "layout", LAYOUTS, lambda a: {"blur": "blurred background", "crop": "zoomed-in crop"}[a])
    lines += ["", "**Length**", ""] + _table(state, "length", LENGTHS, lambda a: f"{a} seconds")
    lines += ["", "**Caption style**", ""] + _table(
        state, "caption", tuple(CAPTION_STYLES),
        lambda a: {"upper3": "3 words, CAPITALS", "upper2": "2 words, CAPITALS", "lower3": "3 words, normal letters"}[a],
    )
    lines += ["", "**Title style**", ""] + _table(
        state, "title", TITLE_STYLES,
        lambda a: {"plain": "original title", "creator": "creator name first", "caps": "ALL CAPITALS"}[a],
    )

    stats = arm_stats(state, "channel")
    ranked = sorted(stats.items(), key=lambda kv: kv[1][1], reverse=True)
    if ranked:
        lines += ["", "## Source channels", "", "| channel | measured Shorts | typical views at 24 h |", "|---|---|---|"]
        for channel_id, (count, avg) in ranked[:8]:
            lines.append(f"| {names.get(channel_id, channel_id)} | {count} | {views_from_score(avg)} |")
    hours = best_hours(state)
    if hours:
        lines += ["", "## Best time to post", "", "Your Shorts did best when posted around (UTC): "
                  + ", ".join(f"{h:02d}:00" for h in sorted(hours)) + "."]
    lessons = recent_lessons(state, 12)
    lines += ["", "## What the bot noticed and changed", ""]
    lines += [f"- {row['ts'][:16].replace('T', ' ')} - {row['detail']}" for row in lessons] or ["- Nothing yet."]
    lines += ["", "_Views are noisy and YouTube decides reach. This report shows tendencies, not promises._", ""]
    return "\n".join(lines)
