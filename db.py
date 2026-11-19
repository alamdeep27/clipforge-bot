"""SQLite state: which videos were seen/processed, what was uploaded, daily counters."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id    TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    title       TEXT,
    published   TEXT,
    status      TEXT NOT NULL,        -- seeded | new | ready | uploaded | skipped | failed
    detail      TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    views_seen  INTEGER                  -- source video's public views at the last poll (momentum)
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

CREATE TABLE IF NOT EXISTS seeded_channels (
    channel_id TEXT PRIMARY KEY,
    seeded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_video_id   TEXT NOT NULL,
    uploaded_video_id TEXT,
    pt_day            TEXT NOT NULL,   -- Pacific-time date: Google's quota resets at midnight PT
    uploaded_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_day ON uploads(pt_day);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- one row per Short the bot made: the choices it made, and later how many views it got
CREATE TABLE IF NOT EXISTS clips (
    source_video_id   TEXT PRIMARY KEY,
    source_channel_id TEXT NOT NULL,
    source_title      TEXT,
    source_views      INTEGER,
    source_age_hours  REAL,
    created_at        TEXT NOT NULL,
    delivered_kind    TEXT,
    my_video_id       TEXT,
    my_title          TEXT,
    posted_at         TEXT,
    layout            TEXT,
    length_arm        TEXT,
    caption_arm       TEXT,
    title_arm         TEXT,
    method            TEXT,
    start_frac        REAL,
    views_6h          INTEGER,
    views_24h         INTEGER,
    views_72h         INTEGER,
    last_views        INTEGER,
    last_likes        INTEGER,
    last_checked      TEXT,
    reward            REAL
);
CREATE INDEX IF NOT EXISTS idx_clips_my ON clips(my_video_id);

-- a plain-English diary of what the bot noticed and changed
CREATE TABLE IF NOT EXISTS lessons (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pt_today() -> str:
    """Today's date in Pacific time (the day boundary YouTube API quotas use)."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # tzdata missing (Windows without the tzdata package)
        tz = timezone(timedelta(hours=-8))
    return datetime.now(tz).date().isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_hours(published: str | None) -> float | None:
    parsed = _parse_time(published)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0


class State:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(videos)")}
        if "views_seen" not in columns:                # database created by an older version
            self.conn.execute("ALTER TABLE videos ADD COLUMN views_seen INTEGER")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- seeding / discovery -------------------------------------------------
    def channel_seeded(self, channel_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seeded_channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row is not None

    def seed_channel(self, channel_id: str, entries) -> int:
        """First poll of a channel: everything already in its feed counts as 'seen'."""
        now = utcnow()
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO videos"
                "(video_id, channel_id, title, published, status, detail, first_seen, updated_at)"
                " VALUES (?, ?, ?, ?, 'seeded', 'already in feed on first poll', ?, ?)",
                [(e.video_id, channel_id, e.title, e.published, now, now) for e in entries],
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO seeded_channels(channel_id, seeded_at) VALUES (?, ?)",
                (channel_id, now),
            )
        return len(entries)

    def known(self, video_id: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone()
            is not None
        )

    def register_new(self, channel_id: str, entries, max_age_hours: float) -> list[str]:
        """Record unseen feed entries. Old ones are stored as skipped so they never come back."""
        now = utcnow()
        fresh: list[str] = []
        with self.conn:
            for e in entries:
                if self.known(e.video_id):
                    continue
                age = age_hours(e.published)
                stale = age is not None and age > max_age_hours
                status, detail = ("skipped", f"stale ({age:.0f}h old)") if stale else ("new", "")
                self.conn.execute(
                    "INSERT OR IGNORE INTO videos"
                    "(video_id, channel_id, title, published, status, detail, first_seen, updated_at, views_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e.video_id, channel_id, e.title, e.published, status, detail, now, now, getattr(e, "views", 0)),
                )
                if not stale:
                    fresh.append(e.video_id)
        return fresh

    def update_views(self, video_id: str, views: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE videos SET views_seen = ? WHERE video_id = ? AND status = 'new'", (int(views), video_id)
            )

    def ensure_video(self, video_id: str, channel_id: str, title: str) -> None:
        now = utcnow()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO videos"
                "(video_id, channel_id, title, published, status, detail, first_seen, updated_at)"
                " VALUES (?, ?, ?, NULL, 'new', 'queued manually', ?, ?)",
                (video_id, channel_id, title, now, now),
            )

    # ---- work queue -----------------------------------------------------------
    def pending(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM videos WHERE status = 'new' ORDER BY updated_at ASC, published ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def waiting(self, limit: int = 500) -> list[sqlite3.Row]:
        """Every video still waiting to be clipped (the learner ranks them)."""
        return self.pending(limit)

    def attempts(self, video_id: str) -> int:
        row = self.conn.execute(
            "SELECT attempts FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def mark(self, video_id: str, status: str, detail: str = "", bump_attempt: bool = False) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE videos SET status = ?, detail = ?, attempts = attempts + ?, updated_at = ?"
                " WHERE video_id = ?",
                (status, detail[:500], 1 if bump_attempt else 0, utcnow(), video_id),
            )

    def touch(self, video_id: str, detail: str = "") -> None:
        """Send a still-pending video to the back of the queue."""
        with self.conn:
            self.conn.execute(
                "UPDATE videos SET detail = ?, updated_at = ? WHERE video_id = ?",
                (detail[:500], utcnow(), video_id),
            )

    # ---- uploads and the daily cap ----------------------------------------------
    def record_upload(
        self, source_video_id: str, uploaded_video_id: str, status: str = "uploaded", detail: str = ""
    ) -> None:
        """Count one finished Short (uploaded to YouTube, or saved as ready) against today's budget."""
        now = utcnow()
        with self.conn:
            self.conn.execute(
                "INSERT INTO uploads(source_video_id, uploaded_video_id, pt_day, uploaded_at)"
                " VALUES (?, ?, ?, ?)",
                (source_video_id, uploaded_video_id, pt_today(), now),
            )
            self.conn.execute(
                "UPDATE videos SET status = ?, detail = ?, updated_at = ? WHERE video_id = ?",
                (status, (detail or f"uploaded as {uploaded_video_id}")[:500], now, source_video_id),
            )

    def uploads_today(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM uploads WHERE pt_day = ?", (pt_today(),)
        ).fetchone()
        return int(row["n"])

    def block_uploads_today(self) -> None:
        self.set_meta("uploads_blocked_pt_day", pt_today())

    def uploads_blocked_today(self) -> bool:
        return self.get_meta("uploads_blocked_pt_day") == pt_today()

    # ---- misc ---------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM videos GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
