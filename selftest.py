#!/usr/bin/env python3
"""selftest.py - verify the bot on THIS computer without touching YouTube.

    python selftest.py

Checks the tools (ffmpeg, yt-dlp, captions, JavaScript runtime), then runs the
real code on synthetic data: RSS parsing, first-run seeding, the channel list and
link resolver, hottest-moment detection, captions, the 1080x1920 render with
loudness normalisation, the finished-file delivery, the upload metadata, the
daily limits, and the whole run loop with the network pieces faked.

It cannot test the live parts: yt-dlp talking to YouTube, speech recognition
(needs the downloaded caption model) and the YouTube upload. Use
`python main.py --check-youtube` and `python main.py --dry-run --video LINK` for those.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import captions
import clipper
import config
import db
import delivery
import detector
import feeds
import learner
import permissions
import resolve
import uploader
from db import State
from permissions import Channel

SAMPLE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <yt:channelId>UC0000000000000000000001</yt:channelId>
 <title>Example Gaming</title>
 <author><name>Example Gaming</name></author>
 <entry>
  <id>yt:video:AAAAAAAAAAA</id><yt:videoId>AAAAAAAAAAA</yt:videoId>
  <yt:channelId>UC0000000000000000000001</yt:channelId>
  <title>Final boss, no damage</title>
  <author><name>Example Gaming</name></author>
  <published>2026-11-16T10:00:00+00:00</published>
  <media:group>
   <media:title>Final boss, no damage</media:title>
   <media:description>Watch the full video: https://www.youtube.com/watch?v=ZZZZZZZZZZZ
Subscribe!</media:description>
   <media:community>
    <media:starRating count="12" average="5.00" min="1" max="5"/>
    <media:statistics views="3456"/>
   </media:community>
  </media:group>
 </entry>
 <entry>
  <id>yt:video:BBBBBBBBBBB</id><yt:videoId>BBBBBBBBBBB</yt:videoId>
  <yt:channelId>UC0000000000000000000001</yt:channelId>
  <title>Older upload</title>
  <author><name>Example Gaming</name></author>
  <published>2020-01-01T00:00:00+00:00</published>
 </entry>
</feed>"""

CHANNEL_PAGE = (
    '<html><head><link rel="canonical" href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">'
    '<meta property="og:title" content="Some &amp; Gamer"></head>'
    '<body>"externalId":"UCzzzzzzzzzzzzzzzzzzzzzz"</body></html>'
)

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        note = fn() or ""
        RESULTS.append(("PASS", name, str(note)))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))


def warn(name: str, note: str) -> None:
    RESULTS.append(("WARN", name, note))


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


# ---- environment ------------------------------------------------------------
def environment() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool):
            RESULTS.append(("PASS", f"{tool} on PATH", run([tool, "-version"]).stdout.splitlines()[0]))
        else:
            RESULTS.append(("FAIL", f"{tool} on PATH", "install ffmpeg (see START-HERE.md)"))
    if shutil.which("ffmpeg"):
        if captions.filter_available():
            RESULTS.append(("PASS", "ffmpeg can burn in captions", "libass 'subtitles' filter present"))
        else:
            warn("ffmpeg can burn in captions", "this ffmpeg has no 'subtitles' filter - Shorts will have no captions")
    for module, hint in (
        ("yt_dlp", "pip install -r requirements.txt"),
        ("faster_whisper", "pip install -r requirements.txt (needed for captions)"),
        ("googleapiclient", "only needed for api delivery"),
        ("google_auth_oauthlib", "only needed for api delivery"),
    ):
        try:
            __import__(module)
            RESULTS.append(("PASS", f"import {module}", ""))
        except ImportError:
            warn(f"import {module}", f"missing - {hint}")
    if not any(shutil.which(r) for r in ("deno", "node", "bun", "qjs")):
        warn("JavaScript runtime", "none found. yt-dlp needs Deno (or Node with YT_JS_RUNTIMES=node) for YouTube")


# ---- unit checks ---------------------------------------------------------------
def t_feed() -> str:
    entries = feeds.parse_feed(SAMPLE_FEED)
    assert [e.video_id for e in entries] == ["AAAAAAAAAAA", "BBBBBBBBBBB"], entries
    assert entries[0].channel_id == "UC0000000000000000000001"
    assert entries[0].title == "Final boss, no damage"
    assert (entries[0].views, entries[0].likes) == (3456, 12), entries[0]
    assert "watch?v=ZZZZZZZZZZZ" in entries[0].description
    assert (entries[1].views, entries[1].likes, entries[1].description) == (0, 0, "")     # entry without stats
    return f"{len(entries)} entries parsed (views, likes and description included)"


def t_seeding(tmp: Path) -> str:
    state = State(tmp / "seed.db")
    entries = feeds.parse_feed(SAMPLE_FEED)
    ch = "UC0000000000000000000001"
    assert not state.channel_seeded(ch)
    state.seed_channel(ch, entries)
    assert state.channel_seeded(ch) and state.counts() == {"seeded": 2}
    newer = entries + [
        feeds.FeedEntry("CCCCCCCCCCC", ch, "Brand new", db.utcnow(), "x"),
        feeds.FeedEntry("DDDDDDDDDDD", ch, "Ancient", "2019-01-01T00:00:00+00:00", "x"),
    ]
    fresh = state.register_new(ch, newer, max_age_hours=72)
    assert fresh == ["CCCCCCCCCCC"], fresh
    assert state.counts() == {"seeded": 2, "new": 1, "skipped": 1}, state.counts()
    assert state.register_new(ch, newer, 72) == []          # never processed twice
    assert [r["video_id"] for r in state.pending(10)] == ["CCCCCCCCCCC"]
    state.close()
    return "first-run seeding, stale filter and de-duplication OK"


def t_resolver() -> str:
    assert resolve.channel_id_in("UCabcdefghijklmnopqrstuv") == "UCabcdefghijklmnopqrstuv"
    assert resolve.channel_id_in("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos") == "UCabcdefghijklmnopqrstuv"
    assert resolve.channel_id_in("@SomeGamer") is None
    assert resolve.to_url("@SomeGamer") == "https://www.youtube.com/@SomeGamer"
    assert resolve.to_url("SomeGamer") == "https://www.youtube.com/@SomeGamer"
    assert resolve.to_url("https://www.youtube.com/@SomeGamer/videos") == "https://www.youtube.com/@SomeGamer"
    assert resolve.to_url("youtube.com/@SomeGamer/streams?x=1") == "https://www.youtube.com/@SomeGamer"
    assert resolve.to_url("https://youtube.com/c/OldName/") == "https://www.youtube.com/c/OldName"
    for bad in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", "https://example.com/@x"):
        try:
            resolve.to_url(bad)
            raise AssertionError(f"accepted {bad}")
        except resolve.ResolveError:
            pass
    channel_id, name = resolve.parse_page(CHANNEL_PAGE)
    assert channel_id == "UCabcdefghijklmnopqrstuv", channel_id        # canonical beats other ids on the page
    assert name == "Some & Gamer", name
    try:
        resolve.parse_page("<html>nothing here</html>")
        raise AssertionError("parsed an empty page")
    except resolve.ResolveError:
        pass
    return "links, @handles and channel pages understood"


def t_channel_list(tmp: Path) -> str:
    text = tmp / "channels.txt"
    text.write_text(
        "\ufeff# my approved channels\n"
        "https://www.youtube.com/@SomeGamer   # said yes by email 2026-11-01\n"
        "@SomeGamer\n"                                         # same channel again
        "UC0000000000000000000002\n"
        "# https://www.youtube.com/@Paused\n"
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"        # a video link, not a channel
        "@NoSuchChannel\n",
        encoding="utf-8",
    )
    lookups: list[str] = []

    def fake_fetch(ref: str):
        lookups.append(ref)
        if "NoSuchChannel" in ref:
            raise resolve.ResolveError("YouTube says that channel does not exist - check the link")
        return "UCabcdefghijklmnopqrstuv", "Some Gamer"

    state = State(tmp / "chan.db")
    channels, problems = permissions.load_channels(text, cache=state, fetch=fake_fetch)
    assert [c.id for c in channels] == ["UCabcdefghijklmnopqrstuv", "UC0000000000000000000002"], channels
    assert channels[0].name == "Some Gamer"
    assert any("watch?v" in p for p in problems) and any("NoSuchChannel" in p for p in problems), problems
    assert any("listed twice" in p for p in problems), problems
    first_lookups = len(lookups)
    permissions.load_channels(text, cache=state, fetch=fake_fetch)       # second load: cached
    assert len(lookups) == first_lookups + 1, lookups                  # only the failing one is retried

    old = tmp / "channels.json"
    old.write_text(json.dumps([{"id": "UC0000000000000000000003", "name": "Old style"}, {"id": "bad"}]))
    legacy, legacy_problems = permissions.load_channels(old)
    assert [c.name for c in legacy] == ["Old style"] and len(legacy_problems) == 1
    state.close()
    return "channels.txt parsed, handles resolved once and cached, bad lines reported"


def heat(peak_index: int, n: int = 100, seg: float = 6.0, first: float = 1.0) -> list[dict]:
    return [
        {"start_time": i * seg, "end_time": (i + 1) * seg,
         "value": first if i == 0 else (0.95 if i == peak_index else 0.3)}
        for i in range(n)
    ]


def t_heatmap() -> str:
    info = {"duration": 600.0, "heatmap": heat(50)}
    w = detector.heatmap_window(info)
    assert w and w.method == "heatmap" and abs(w.peak - 303.0) < 0.01, w
    assert 45 <= w.duration <= 58, w.duration
    assert abs((w.start + w.end) / 2 - 303.0) < 0.5 and w.start >= 15
    late = detector.heatmap_window({"duration": 600.0, "heatmap": heat(99)})
    assert late and late.end <= 598.0 and late.peak < 590, late
    early = detector.heatmap_window({"duration": 600.0, "heatmap": heat(3)})
    assert early and early.start >= 15.0, early
    assert detector.heatmap_window({"duration": 600.0}) is None
    assert detector.heatmap_window({"duration": 40.0, "heatmap": heat(2, n=6)}) is None
    return f"peak 303s -> window {w.start}-{w.end}s ({w.duration}s)"


def t_timestamps() -> str:
    assert detector.timestamps_in("OMG 3:42 and 1:02:03 !!") == [222, 3723]
    assert detector.timestamps_in("score 99:99 or 12:75") == []
    counts = detector.comment_curve([{"text": "1:00 insane", "like_count": 50}], 400, 200.0)
    assert counts.max() > 0 and counts[120] == counts.max()
    assert counts[100] == 0 and counts[140] == 0
    return "comment timestamps parsed"


def t_metadata() -> str:
    info = {"id": "AAAAAAAAAAA", "title": "x" * 150 + " <b>", "channel": "Example Gaming",
            "channel_url": "https://www.youtube.com/channel/UC0000000000000000000001"}
    window = detector.Window(272.5, 327.5, "heatmap", 300.0, 0.9)
    ch = Channel("UC0000000000000000000001", "Example Gaming")
    body = uploader.build_body(info, ch, window, "private")
    title, desc = body["snippet"]["title"], body["snippet"]["description"]
    assert len(title) <= 100 and title.endswith("#Shorts") and "<" not in title, title
    assert body["snippet"]["categoryId"] == "20"
    assert "https://www.youtube.com/watch?v=AAAAAAAAAAA" in desc and "Example Gaming" in desc
    assert "&t=272s" in desc and "#Shorts" in desc and "permission" in desc
    assert "ClipForge" in desc and "https://www.youtube.com/@ClipForge-n3b" in desc
    assert "<" not in desc and len(desc.encode()) < 5000
    assert "ClipForge" in body["snippet"]["tags"] and body["status"]["privacyStatus"] == "private"
    try:
        uploader.build_body(info, ch, window, "everyone")
        raise AssertionError("invalid privacy accepted")
    except ValueError:
        pass
    return "title <=100 chars, #Shorts, category 20, creator credit, ClipForge footer"


def t_caps(tmp: Path) -> str:
    state = State(tmp / "cap.db")
    state.ensure_video("AAAAAAAAAAA", "UC0000000000000000000001", "t")
    saved = (config.DELIVERY, config.MAX_UPLOADS_PER_DAY, config.MAX_CLIPS_PER_DAY)
    try:
        config.DELIVERY, config.MAX_CLIPS_PER_DAY, config.MAX_UPLOADS_PER_DAY = "files", 2, 90
        assert uploader.cap_status(state)[0]
        state.record_upload("AAAAAAAAAAA", "", status="ready")
        state.record_upload("AAAAAAAAAAA", "", status="ready")
        ok, why = uploader.cap_status(state)
        assert not ok and "2/2" in why and "clips" in why, why
        config.DELIVERY = "api"                       # a different budget applies to uploads
        assert uploader.cap_status(state)[0]
        state.block_uploads_today()
        ok, why = uploader.cap_status(state)
        assert not ok and "quota" in why, why
        config.DELIVERY = "files"                     # the quota flag only matters for uploads
        assert not uploader.cap_status(state)[0] and "quota" not in uploader.cap_status(state)[1]
    finally:
        config.DELIVERY, config.MAX_UPLOADS_PER_DAY, config.MAX_CLIPS_PER_DAY = saved
    state.close()
    return "separate daily limits for finished files and for uploads"


def t_captions_text() -> str:
    assert captions.clean_word(" Hello, ") == "HELLO" and captions.clean_word("don't") == "DON'T"
    assert captions.clean_word("{\\b1}") == "B1" and captions.clean_word("...") == ""
    words = [captions.Word(t, 1.0 + i * 0.4, 1.35 + i * 0.4) for i, t in enumerate(
        "THIS IS THE CRAZIEST CLUTCH YOU WILL EVER SEE".split())]
    words.append(captions.Word("AFTER", 9.0, 9.3))                     # a long pause forces a new line
    chunks = captions.chunk_words(words)
    assert all(len(c) <= 3 for c in chunks) and all(sum(len(w.text) + 1 for w in c) <= 17 for c in chunks), chunks
    assert chunks[-1][0].text == "AFTER" and len(chunks[-1]) == 1
    ass = captions.build_ass(words)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "Style: Cap,Poppins," in ass
    lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(lines) == len(words), len(lines)

    def secs(stamp: str) -> float:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    spans = [(secs(ln.split(",")[1]), secs(ln.split(",")[2])) for ln in lines]
    assert all(end > start for start, end in spans), spans
    assert all(spans[i][1] <= spans[i + 1][0] + 0.011 for i in range(len(spans) - 1)), "captions overlap"
    assert lines[0].count("{\\c&H00FFFF&}") == 1 and "{\\c&HFFFFFF&}" in lines[0]       # one yellow word at a time
    assert captions.ass_time(3723.456) == "1:02:03.46"
    return f"{len(chunks)} caption lines, {len(lines)} timed words, no overlaps"


def make_sample(path: Path, seconds: int = 100, burst: tuple[int, int] = (55, 70)) -> None:
    """16:9 test pattern with quiet pink noise and one loud 15 s 'hype' burst."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.9:duration={seconds}:sample_rate=48000",
        "-af", f"volume='if(between(t,{burst[0]},{burst[1]}),1.0,0.04)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-300:])


def t_audio(sample: Path) -> str:
    info = {"duration": 100.0}
    w = detector.find_best_window(info, "local", sample.parent, local_media=sample)
    assert w.method == "audio", w
    assert w.start <= 56 and w.end >= 69, f"burst 55-70s not inside {w.start}-{w.end}"
    assert 57 <= w.peak <= 68, w.peak
    assert 45 <= w.duration <= 58
    return f"burst at 55-70s found: window {w.start}-{w.end}s via {w.method}"


def probe_loudness(path: Path) -> float:
    proc = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn",
                "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    start = proc.stderr.rfind("{")
    return float(json.loads(proc.stderr[start:proc.stderr.rfind("}") + 1])["input_i"])


def t_render(sample: Path, tmp: Path, layout: str) -> str:
    config.X264_PRESET = "veryfast"
    out = tmp / f"short_{layout}.mp4"
    measured = clipper.measure_loudness(sample, 60.0, 8.0)
    assert measured is not None
    clipper.render(sample, out, 60.0, 8.0, layout, measured)
    meta = clipper.probe(out)
    w, h = clipper.video_size(meta)
    length = clipper.duration_of(meta)
    assert (w, h) == (1080, 1920) and length < 59 and clipper.has_audio(meta), (w, h, length)
    lufs = probe_loudness(out)
    assert abs(lufs - config.TARGET_LUFS) < 2.5, f"loudness {lufs} LUFS, wanted ~{config.TARGET_LUFS}"
    return f"{w}x{h}, {length:.1f}s, {lufs:.1f} LUFS"


def t_make_short(sample: Path, tmp: Path) -> str:
    """The real make_short() path on a full-length window, captions off (no speech in the sample)."""
    saved = config.CAPTIONS
    config.CAPTIONS = False
    try:
        work = tmp / "work"
        window = detector.Window(30.0, 85.0, "audio", 57.5, 1.0)
        out = clipper.make_short("local", window, tmp / "full.mp4", work_dir=work, layout="blur", local_media=sample)
    finally:
        config.CAPTIONS = saved
    meta = clipper.probe(out)
    assert clipper.video_size(meta) == (1080, 1920) and clipper.duration_of(meta) < 59
    assert abs(probe_loudness(out) - config.TARGET_LUFS) < 2.5
    return f"{clipper.duration_of(meta):.1f}s Short rendered"


def caption_region(path: Path, at: float) -> np.ndarray:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(path), "-frames:v", "1",
         "-vf", "crop=1080:260:0:1350,format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.uint8).astype(np.float32)


def t_caption_burn(sample: Path, tmp: Path) -> str:
    config.X264_PRESET = "veryfast"
    work = tmp / "capwork"
    work.mkdir(exist_ok=True)
    words = [captions.Word(t, 1.0 + i * 0.35, 1.3 + i * 0.35) for i, t in enumerate(
        "THIS IS WHAT A PERFECT CLUTCH LOOKS LIKE WOW".split())]
    (work / "captions.ass").write_text(captions.build_ass(words), encoding="utf-8")
    captions.install_font(work)
    assert (work / "fonts" / captions.FONT_FILE).exists()
    plain, with_caps = tmp / "plain.mp4", tmp / "with_caps.mp4"
    clipper.render(sample, plain, 60.0, 6.0, "blur", None)
    clipper.render(sample, with_caps, 60.0, 6.0, "blur", None, ass=work / "captions.ass")
    diff = float(np.mean(np.abs(caption_region(plain, 1.5) - caption_region(with_caps, 1.5))))
    assert diff > 3.0, f"captions not visible in the caption area (mean difference {diff:.2f})"
    quiet = float(np.mean(np.abs(caption_region(plain, 5.5) - caption_region(with_caps, 5.5))))
    assert quiet < diff / 3, f"captions still showing after the last word ({quiet:.2f})"
    return f"captions burned in (area difference {diff:.1f}), gone when nobody speaks ({quiet:.1f})"



# ---- learning loop ---------------------------------------------------------------
_clip_n = [0]


def add_clip(state, channel_id="UC0000000000000000000001", reward=None, layout="blur", length="53",
             caption="upper3", title="plain", posted_at=None, source=None, my_title=None):
    _clip_n[0] += 1
    sid = source or f"S{_clip_n[0]:010d}"
    state.conn.execute(
        "INSERT INTO clips(source_video_id, source_channel_id, created_at, layout, length_arm, caption_arm,"
        " title_arm, method, reward, posted_at, my_title) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, channel_id, db.utcnow(), layout, length, caption, title, "audio", reward, posted_at, my_title),
    )
    state.conn.commit()
    return sid


def t_titles() -> str:
    assert uploader.build_title("Boss fight", "plain", "Ex Gamer") == "Boss fight #Shorts"
    assert uploader.build_title("Boss fight", "creator", "Ex Gamer") == "Ex Gamer: Boss fight #Shorts"
    assert uploader.build_title("Boss fight", "caps", "Ex Gamer") == "BOSS FIGHT #Shorts"
    assert len(uploader.build_title("x" * 300, "creator", "Ex Gamer")) <= 100
    return "title styles plain / creator / caps, always within 100 characters"


def t_bandit(tmp: Path) -> str:
    """The bot is never told which options are better; it must find out from the views alone."""
    import random

    effects = {
        "layout": {"blur": 0.0, "crop": 0.5},
        "length": {"48": 0.0, "53": 0.1, "58": 0.4},
        "caption": {"upper3": 0.0, "upper2": 0.5, "lower3": -0.2},
        "title": {"plain": 0.0, "creator": 0.4, "caps": -0.1},
    }
    best = {"layout": "crop", "length": "58", "caption": "upper2", "title": "creator"}
    saved = (config.FIXED, config.LEARNING)
    config.FIXED, config.LEARNING = set(), True
    shares = {k: [] for k in best}
    try:
        for seed in (1, 2, 3):
            rng = random.Random(seed)
            state = State(tmp / f"bandit{seed}.db")
            picks = []
            for _ in range(300):
                plan = learner.choose_plan(state, rng)
                score = 1.5 + effects["layout"][plan.layout] + effects["length"][plan.length] \
                    + effects["caption"][plan.caption] + effects["title"][plan.title] + rng.gauss(0, 0.45)
                add_clip(state, reward=score, layout=plan.layout, length=plan.length, caption=plan.caption, title=plan.title)
                picks.append(plan)
            last = picks[-100:]
            for dim, attr in (("layout", "layout"), ("length", "length"), ("caption", "caption"), ("title", "title")):
                shares[dim].append(sum(getattr(p, attr) == best[dim] for p in last) / len(last))
            state.close()
    finally:
        config.FIXED, config.LEARNING = saved
    floor = {"layout": 0.75, "length": 0.5, "caption": 0.65, "title": 0.55}
    for dim, values in shares.items():
        mean = sum(values) / len(values)
        assert mean >= floor[dim], f"{dim}: picked the best option only {mean:.0%} of the time {values}"
    return ", ".join(f"{d} {sum(v) / len(v):.0%}" for d, v in shares.items()) + " of the last 100 picks used the truly best option"


def t_exploring(tmp: Path) -> str:
    """With no data the bot must try everything, not lock onto one option."""
    import random

    state = State(tmp / "explore.db")
    rng = random.Random(5)
    config_saved = (config.FIXED, config.LEARNING)
    config.FIXED, config.LEARNING = set(), True
    try:
        layouts = {learner.choose_plan(state, rng).layout for _ in range(60)}
        captions_seen = {learner.choose_plan(state, rng).caption for _ in range(60)}
    finally:
        config.FIXED, config.LEARNING = config_saved
    assert layouts == set(learner.LAYOUTS) and captions_seen == set(learner.CAPTION_STYLES), (layouts, captions_seen)
    state.close()
    return "with no data every option still gets tried"


def t_plan_overrides(tmp: Path) -> str:
    import random

    state = State(tmp / "fixed.db")
    saved = (config.FIXED, config.LEARNING, config.LAYOUT, config.CAPTION_WORDS, config.CAPTION_UPPER)
    try:
        config.LEARNING, config.FIXED, config.LAYOUT = True, {"layout"}, "crop"
        rng = random.Random(3)
        assert {learner.choose_plan(state, rng).layout for _ in range(40)} == {"crop"}     # you said crop: crop it is
        config.FIXED, config.CAPTION_WORDS, config.CAPTION_UPPER = {"caption"}, 4, False
        plan = learner.choose_plan(state, rng)
        assert (plan.caption_words, plan.caption_upper) == (4, False), plan                 # custom captions respected
        config.LEARNING = False
        assert learner.choose_plan(state, rng) == learner.default_plan()                    # learning switched off
    finally:
        config.FIXED, config.LEARNING, config.LAYOUT, config.CAPTION_WORDS, config.CAPTION_UPPER = saved
    state.close()
    return "settings you choose yourself are never overridden; YT_LEARNING=0 turns learning off"


def t_ranking(tmp: Path) -> str:
    import random

    state = State(tmp / "rank.db")
    good, poor, fresh = "UC000000000000000000000G", "UC000000000000000000000P", "UC000000000000000000000N"
    for i in range(8):
        add_clip(state, good, 2.9 + 0.05 * i)
        add_clip(state, poor, 0.9 + 0.05 * i)
    now = db.utcnow()
    state.register_new(good, [feeds.FeedEntry("GGGGGGGGGGG", good, "g", now, "g", views=50)], 72)
    state.register_new(poor, [feeds.FeedEntry("PPPPPPPPPPP", poor, "p", now, "p", views=50)], 72)
    rows = state.waiting()
    wins = sum(learner.rank_pending(state, rows, random.Random(i))[0]["video_id"] == "GGGGGGGGGGG" for i in range(200))
    assert wins >= 190, f"the proven channel came first only {wins}/200 times"

    # same unknown channel, one source video is taking off and one is not: the rising one goes first
    state.register_new(fresh, [feeds.FeedEntry("HOTHOTHOTHO", fresh, "hot", now, "n", views=90000),
                               feeds.FeedEntry("COLDCOLDCOL", fresh, "cold", now, "n", views=0)], 72)
    only = [r for r in state.waiting() if r["channel_id"] == fresh]
    hot = sum(learner.rank_pending(state, only, random.Random(i))[0]["video_id"] == "HOTHOTHOTHO" for i in range(200))
    assert hot >= 170, f"the rising video came first only {hot}/200 times"
    state.close()
    return f"proven channel first {wins}/200, rising source video first {hot}/200"


def t_mistakes(tmp: Path) -> str:
    import random

    state = State(tmp / "mistakes.db")
    bad = Channel("UC000000000000000000000B", "Broken Channel")
    for i in range(4):
        state.conn.execute(
            "INSERT INTO videos(video_id, channel_id, status, first_seen, updated_at) VALUES (?,?,?,?,?)",
            (f"FAIL{i:07d}", bad.id, "failed", db.utcnow(), db.utcnow()),
        )
    state.conn.commit()
    learner.after_outcome(state, bad, "failed")
    assert learner.is_paused(state, bad.id)
    assert "paused Broken Channel" in learner.recent_lessons(state)[0]["detail"]
    row = {"video_id": "XXXXXXXXXXX", "channel_id": bad.id, "views_seen": 0, "published": db.utcnow()}
    assert learner.rank_pending(state, [row], random.Random(1)) == []             # paused channels are skipped
    state.set_meta(f"pause:{bad.id}", "2000-01-01T00:00:00+00:00")                # the pause runs out
    assert not learner.is_paused(state, bad.id)
    assert any(l["kind"] == "resumed" for l in learner.recent_lessons(state))

    # a channel whose Shorts get far fewer views than the rest is rotated out for a while
    weak, strong = Channel("UC000000000000000000000W", "Weak Source"), Channel("UC000000000000000000000S", "Strong Source")
    for i in range(7):
        add_clip(state, weak.id, 0.7 + 0.02 * i)
        add_clip(state, strong.id, 2.6 + 0.02 * i)
    learner.review_channels(state, [weak, strong])
    assert learner.is_paused(state, weak.id) and not learner.is_paused(state, strong.id)
    assert "views" in learner.recent_lessons(state)[0]["detail"]
    state.close()
    return "repeated failures and weak sources get paused (and resume later); every step is written to the diary"


def t_tracking(tmp: Path) -> str:
    from datetime import datetime, timedelta, timezone

    state = State(tmp / "track.db")
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    iso = lambda hours_ago: (now - timedelta(hours=hours_ago)).isoformat()
    c1 = add_clip(state, reward=None, source="AAAAAAAAAAA")                                   # matched by description link
    c2 = add_clip(state, reward=None, source="BBBBBBBBBBB", my_title="Another one #Shorts")   # matched by title
    entries = [
        feeds.FeedEntry("MY1MY1MY1MY", "UCmine", "edited title", iso(7), "me", views=120, likes=9,
                        description="Watch: https://www.youtube.com/watch?v=AAAAAAAAAAA&t=40s"),
        feeds.FeedEntry("MY2MY2MY2MY", "UCmine", "Another one #Shorts", iso(30), "me", views=900),
        feeds.FeedEntry("OTHEROTHERO", "UCmine", "unrelated upload", iso(30), "me", views=5, description="nothing"),
    ]
    result = learner.ingest_feed(state, entries, now)
    assert result == {"matched": 2, "scored": 1}, result
    r1 = state.conn.execute("SELECT * FROM clips WHERE source_video_id=?", (c1,)).fetchone()
    assert (r1["my_video_id"], r1["views_6h"], r1["views_24h"], r1["reward"]) == ("MY1MY1MY1MY", 120, None, None)
    r2 = state.conn.execute("SELECT * FROM clips WHERE source_video_id=?", (c2,)).fetchone()
    assert r2["views_24h"] == 900 and abs(r2["reward"] - math.log10(901)) < 1e-9, dict(r2)

    later = [feeds.FeedEntry("MY1MY1MY1MY", "UCmine", "edited title", iso(7), "me", views=1000, description="x")]
    learner.ingest_feed(state, later, now + timedelta(hours=20))                              # 27 hours old now
    r1 = state.conn.execute("SELECT * FROM clips WHERE source_video_id=?", (c1,)).fetchone()
    assert r1["views_6h"] == 120 and r1["views_24h"] == 1000 and abs(r1["reward"] - math.log10(1001)) < 1e-9, dict(r1)
    assert r1["last_views"] == 1000
    state.close()
    return "posted Shorts matched by link or title; views saved at 6/24/72 h; score = log10(1 + views at 24 h)"


def t_track_throttle(tmp: Path) -> str:
    state = State(tmp / "throttle.db")
    calls = []
    fetch = lambda channel_id: calls.append(channel_id) or []
    saved = config.LEARNING
    config.LEARNING = True
    try:
        learner.track_results(state, fetch=fetch)
        learner.track_results(state, fetch=fetch)                      # within 30 minutes: no second request
        learner.track_results(state, fetch=fetch, force=True)
    finally:
        config.LEARNING = saved
    assert calls == [config.MY_CHANNEL_ID, config.MY_CHANNEL_ID], calls
    state.close()
    return "your channel feed is read at most every 30 minutes (one tiny request)"


def t_hours_and_report(tmp: Path) -> str:
    from datetime import datetime, timedelta, timezone

    state = State(tmp / "report.db")
    base = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    for day in range(6):
        add_clip(state, reward=3.0, posted_at=(base + timedelta(days=day, hours=15)).isoformat(), layout="crop")
        add_clip(state, reward=1.0, posted_at=(base + timedelta(days=day, hours=3)).isoformat(), layout="blur")
    assert learner.best_hours(state)[0] == 15, learner.best_hours(state)
    assert "15:00" in learner.post_time_hint(state)
    text = learner.report(state, [Channel("UC0000000000000000000001", "Example Gaming")])
    for needle in ("learning report", "still exploring", "best so far", "zoomed-in crop", "Best time to post", "15:00"):
        assert needle in text, needle
    assert "Example Gaming" in text and "views" in text
    assert "no Shorts made yet" in learner.summary_line(State(tmp / "empty.db"))
    state.close()
    return "report shows the winners, the best posting hour, and honest 'still exploring' status"


def t_clip_record(tmp: Path) -> str:
    state = State(tmp / "record.db")
    window = detector.Window(100.0, 155.0, "heatmap", 127.5, 1.0)
    plan = learner.Plan(layout="crop", length="53", caption="upper2", title="creator")
    delivered = delivery.Delivered("ready", "x.mp4", title="Great #Shorts")
    info = {"id": "RECRECRECRE", "title": "Great", "duration": 600.0, "view_count": 1234, "timestamp": time.time() - 7200}
    learner.record_clip(state, info, Channel("UC0000000000000000000001", "Ex"), window, plan, delivered)
    row = state.conn.execute("SELECT * FROM clips WHERE source_video_id='RECRECRECRE'").fetchone()
    assert (row["layout"], row["length_arm"], row["caption_arm"], row["title_arm"]) == ("crop", "53", "upper2", "creator")
    assert row["my_title"] == "Great #Shorts" and row["source_views"] == 1234 and 1.9 < row["source_age_hours"] < 2.1
    assert abs(row["start_frac"] - 100 / 600) < 1e-9 and row["method"] == "heatmap"
    state.close()
    return "every Short is stored with the choices behind it"



def t_flow(tmp: Path) -> str:
    """Whole run loop (seed -> queue -> clip -> deliver -> clean up) with the network pieces faked."""
    from types import SimpleNamespace

    import main as pipeline

    ch1, ch2 = "UC0000000000000000000001", "UC0000000000000000000002"
    ok_cc = {"title": "Good one", "duration": 600.0, "channel": "One", "channel_id": ch1}
    infos = {
        "CCCCCCCCCCC": {"id": "CCCCCCCCCCC", **ok_cc},
        "SSSSSSSSSSS": {"id": "SSSSSSSSSSS", **ok_cc, "duration": 30.0},
        "FFFFFFFFFFF": {"id": "FFFFFFFFFFF", **ok_cc, "channel": "Two", "channel_id": ch2},
        "BBBBBBBBBBB": {"id": "BBBBBBBBBBB", **ok_cc, "channel": "Two", "channel_id": ch2},
        "GGGGGGGGGGG": {"id": "GGGGGGGGGGG", **ok_cc},
        "HHHHHHHHHHH": {"id": "HHHHHHHHHHH", **ok_cc},
    }
    polls = {"n": 0}

    def fake_poll(ids, workers=8):
        polls["n"] += 1
        old1 = feeds.FeedEntry("OOOOOOOOOOO", ids[0], "old upload", "2026-11-01T00:00:00+00:00", "One")
        old2 = feeds.FeedEntry("QQQQQQQQQQQ", ids[1], "old upload", "2026-11-01T00:00:00+00:00", "Two")
        out = {ids[0]: [old1], ids[1]: [old2]}
        if polls["n"] >= 2:
            fresh = db.utcnow()
            out[ids[0]] = [old1, feeds.FeedEntry("CCCCCCCCCCC", ids[0], "Good one", fresh, "One"),
                           feeds.FeedEntry("SSSSSSSSSSS", ids[0], "Too short", fresh, "One")]
            out[ids[1]] = [old2, feeds.FeedEntry("FFFFFFFFFFF", ids[1], "Will fail", fresh, "Two")]
        return out

    def fake_make_short(ref, window, out_path, *, work_dir, layout=None, local_media=None, **kwargs):
        vid = ytdlp_mod.video_id_from(ref)
        if vid == "FFFFFFFFFFF":
            raise clipper.ClipError("simulated ffmpeg failure")
        if vid == "BBBBBBBBBBB":
            raise ytdlp_mod.YtDlpError("ERROR: [youtube] Sign in to confirm you're not a bot")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake mp4")
        return out_path

    import ytdlp as ytdlp_mod

    saved = (feeds.poll_all, ytdlp_mod.fetch_info, detector.find_best_window, clipper.make_short,
             uploader.get_credentials, uploader._insert, config.TMP_DIR, config.OUTPUT_DIR, config.READY_DIR,
             config.DELIVERY, config.MAX_CLIPS_PER_DAY, config.MAX_UPLOADS_PER_DAY)
    try:
        config.TMP_DIR, config.OUTPUT_DIR, config.READY_DIR = tmp / "f-tmp", tmp / "f-out", tmp / "f-ready"
        config.DELIVERY, config.MAX_CLIPS_PER_DAY = "files", 50
        feeds.poll_all = fake_poll
        ytdlp_mod.fetch_info = lambda ref, **kw: dict(infos[ytdlp_mod.video_id_from(ref)])
        detector.find_best_window = lambda info, ref, work, local_media=None, length=None: detector.Window(100.0, 155.0, "heatmap", 127.5, 1.0)
        clipper.make_short = fake_make_short

        state = State(tmp / "flow.db")
        channels = [Channel(ch1, "One"), Channel(ch2, "Two")]
        args = SimpleNamespace(max_videos=10, layout="blur", privacy="private", seed_only=False)

        pipeline.poll_and_register(state, channels, seed_only=False, dry_run=False)      # run 1: seed
        assert state.counts() == {"seeded": 2}, state.counts()
        assert pipeline.process_pending(state, channels, args) == {}

        out_file = tmp / "gh_output.txt"
        os.environ["GITHUB_OUTPUT"] = str(out_file)
        pipeline.run_cycle(args, state, channels, poll=True, process=False)               # run 2: poll only
        assert "has_work=true" in out_file.read_text(), out_file.read_text()
        assert state.counts() == {"seeded": 2, "new": 3}, state.counts()

        stats = pipeline.process_pending(state, channels, args)
        assert stats["delivered"] == 1 and stats["skipped"] == 1 and stats["failed"] == 1, dict(stats)
        status = {r["video_id"]: r["status"] for r in state.conn.execute("SELECT video_id, status FROM videos")}
        assert status["CCCCCCCCCCC"] == "ready" and status["SSSSSSSSSSS"] == "skipped", status
        assert status["FFFFFFFFFFF"] == "new", status
        ready = sorted(p.name for p in (tmp / "f-ready").iterdir())
        assert len(ready) == 2 and ready[0].endswith(".mp4") and ready[1].endswith(".txt"), ready
        note = (tmp / "f-ready" / ready[1]).read_text(encoding="utf-8")
        assert "TITLE" in note and "Subscribe to ClipForge" in note and "#Shorts" in note, note
        assert not list((tmp / "f-out").glob("*.mp4")) and not (tmp / "f-tmp" / "CCCCCCCCCCC").exists()
        assert state.uploads_today() == 1

        pipeline.process_pending(state, channels, args)                                   # failure retries
        pipeline.process_pending(state, channels, args)
        status = {r["video_id"]: r["status"] for r in state.conn.execute("SELECT video_id, status FROM videos")}
        assert status["FFFFFFFFFFF"] == "failed", status

        state.ensure_video("BBBBBBBBBBB", ch2, "blocked one")                              # YouTube refuses
        stats = pipeline.process_pending(state, channels, args)
        assert stats["blocked"] == 1, dict(stats)
        row = state.conn.execute("SELECT status, attempts FROM videos WHERE video_id='BBBBBBBBBBB'").fetchone()
        assert (row["status"], row["attempts"]) == ("new", 0), dict(row)                   # kept for later, not failed

        state.mark("BBBBBBBBBBB", "skipped", "test done")
        config.MAX_CLIPS_PER_DAY = state.uploads_today() + 1                               # daily clip limit
        for vid in ("GGGGGGGGGGG", "HHHHHHHHHHH"):
            state.ensure_video(vid, ch1, "extra")
        pipeline.process_pending(state, channels, args)
        assert len(state.pending(10)) == 1 and state.uploads_today() == config.MAX_CLIPS_PER_DAY

        # api delivery: same loop, but the finished Short goes through the (faked) YouTube upload
        config.DELIVERY, config.MAX_UPLOADS_PER_DAY = "api", state.uploads_today() + 5
        uploader.get_credentials = lambda **kw: object()
        uploader._insert = lambda creds, path, body: {"id": "UPLOADED001", "status": {"privacyStatus": "private"}}
        stats = pipeline.process_pending(state, channels, args)
        assert stats["delivered"] == 1, dict(stats)
        row = state.conn.execute("SELECT status, detail FROM videos WHERE status='uploaded'").fetchone()
        assert row and "UPLOADED001" in row["detail"], row and dict(row)
        state.close()
    finally:
        os.environ.pop("GITHUB_OUTPUT", None)
        (feeds.poll_all, ytdlp_mod.fetch_info, detector.find_best_window, clipper.make_short,
         uploader.get_credentials, uploader._insert, config.TMP_DIR, config.OUTPUT_DIR, config.READY_DIR,
         config.DELIVERY, config.MAX_CLIPS_PER_DAY, config.MAX_UPLOADS_PER_DAY) = saved
    return "seed -> queue -> clip -> ready files / upload -> retries -> YouTube block -> daily limit"


def t_check_youtube(tmp: Path) -> str:
    """The cloud test must say clearly what is wrong, and only fail for real requirements."""
    import contextlib
    import io

    import main as pipeline
    import resolve
    import ytdlp

    def fake_which(name):
        return f"/usr/bin/{name}" if name in present else None

    def fake_run(args, timeout):
        template = args[args.index("-o") + 1]
        Path(template.replace("%(ext)s", "mp4")).write_bytes(b"x" * 5000)

    def run_check(env_github: bool = False):
        out = io.StringIO()
        old = os.environ.get("GITHUB_ACTIONS")
        if env_github:
            os.environ["GITHUB_ACTIONS"] = "true"
        try:
            with contextlib.redirect_stdout(out):
                code = pipeline.check_youtube()
        finally:
            os.environ.pop("GITHUB_ACTIONS", None)
            if old is not None:
                os.environ["GITHUB_ACTIONS"] = old
        return code, out.getvalue()

    saved = (shutil.which, ytdlp.fetch_info, ytdlp.run, resolve.resolve_channel, config.CHANNELS_FILE)
    present = {"ffmpeg", "deno"}
    try:
        shutil.which = fake_which
        ytdlp.fetch_info = lambda *a, **k: {"id": "x"}
        ytdlp.run = fake_run
        config.CHANNELS_FILE = tmp / "check-channels.txt"
        config.CHANNELS_FILE.write_text("@SomeGamer\n")
        resolve.resolve_channel = lambda ref: (_ for _ in ()).throw(resolve.ResolveError("consent page"))

        code, text = run_check(env_github=True)
        assert code == 0, text                                   # unreadable channel links are only a NOTE
        assert "[NOTE]" in text and "CAN run the bot" in text and "::notice title=ClipForge check::" in text, text

        ytdlp.run = lambda args, timeout: (_ for _ in ()).throw(ytdlp.YtDlpError("Sign in to confirm you're not a bot"))
        code, text = run_check(env_github=True)
        assert code == 1 and "BLOCKING" in text and "Nothing is wrong with your setup" in text, text
        assert "::error title=ClipForge check::" in text and "%0A" not in text.split("::error")[1].split("\n")[0], text

        ytdlp.fetch_info = lambda *a, **k: (_ for _ in ()).throw(ytdlp.YtDlpError("ERROR: HTTP Error 429"))
        code, text = run_check()
        assert code == 1 and "could not talk to YouTube" in text and "BLOCKING" not in text, text

        present.discard("ffmpeg")
        ytdlp.fetch_info = lambda *a, **k: {"id": "x"}
        ytdlp.run = fake_run
        code, text = run_check()
        assert code == 1 and "[PROBLEM] ffmpeg" in text, text
    finally:
        shutil.which, ytdlp.fetch_info, ytdlp.run, resolve.resolve_channel, config.CHANNELS_FILE = saved
    return "OK / blocked / other failure / missing tool are told apart; optional things never fail it"


def t_no_channels(tmp: Path) -> str:
    """An empty channel list must give a friendly message and a green run, not a red cross."""
    import contextlib
    import io

    import main as pipeline

    saved = (config.CHANNELS_FILE, config.DB_PATH, config.TMP_DIR, config.LOG_FILE)
    gh_out, summary = tmp / "empty_out.txt", tmp / "empty_summary.md"
    gh_out.write_text(""); summary.write_text("")
    try:
        config.CHANNELS_FILE = tmp / "empty-channels.txt"
        config.CHANNELS_FILE.write_text("# nothing here yet\n\n")
        config.DB_PATH, config.TMP_DIR, config.LOG_FILE = tmp / "empty.db", tmp / "empty-tmp", tmp / "empty.log"
        os.environ.update(GITHUB_OUTPUT=str(gh_out), GITHUB_STEP_SUMMARY=str(summary), GITHUB_ACTIONS="true")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pipeline.main(["--poll-only"])
            code_normal = pipeline.main([])
        assert code == 0 and code_normal == 0, (code, code_normal)
        assert "has_work=false" in gh_out.read_text()
        assert "No channels yet" in summary.read_text() and "::warning title=No channels yet::" in out.getvalue()
        config.CHANNELS_FILE.unlink()                                   # no channels.txt at all: same friendly result
        with contextlib.redirect_stdout(io.StringIO()):
            assert pipeline.main(["--poll-only"]) == 0
    finally:
        for key in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_ACTIONS"):
            os.environ.pop(key, None)
        config.CHANNELS_FILE, config.DB_PATH, config.TMP_DIR, config.LOG_FILE = saved
        logging.disable(logging.CRITICAL)
    return "no channels yet -> warning at the top of the run page, run stays green"


def t_annotate() -> str:
    import contextlib
    import io

    import main as pipeline

    out = io.StringIO()
    os.environ.pop("GITHUB_ACTIONS", None)
    with contextlib.redirect_stdout(out):
        pipeline.annotate("error", "t", "m")
    assert out.getvalue() == "", "annotations must stay silent outside GitHub"
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        with contextlib.redirect_stdout(out):
            pipeline.annotate("warning", "Title: with, odd chars", "50% done\nsecond line")
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)
    assert out.getvalue().strip() == "::warning title=Title%3A with%2C odd chars::50%25 done%0Asecond line", out.getvalue()
    return "GitHub run-page messages are escaped correctly and silent elsewhere"


def t_cookies(tmp: Path) -> str:
    import main as pipeline

    saved = (config.COOKIES_TEXT, config.COOKIES_FILE, config.TMP_DIR)
    try:
        config.COOKIES_TEXT, config.COOKIES_FILE, config.TMP_DIR = "# Netscape HTTP Cookie File\n", None, tmp
        pipeline.setup_cookies()
        assert config.COOKIES_FILE and Path(config.COOKIES_FILE).read_text().startswith("# Netscape")
        import ytdlp

        assert "--cookies" in ytdlp.base_cmd()
    finally:
        config.COOKIES_TEXT, config.COOKIES_FILE, config.TMP_DIR = saved
    return "cookies secret turned into a file for yt-dlp"


def t_lock(tmp: Path) -> str:
    import main as pipeline

    lock = tmp / "run.lock"
    with pipeline.single_instance(lock):
        assert lock.exists()
        try:
            with pipeline.single_instance(lock):
                raise AssertionError("a second run was allowed while the first was active")
        except pipeline.AlreadyRunning:
            pass
    assert not lock.exists()
    if os.name == "posix":                         # a crashed run's lock is recovered immediately
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        lock.write_text(str(done.pid))
        with pipeline.single_instance(lock):
            pass
    return "overlapping runs blocked; lock from a dead run recovered"


def t_caption_pipeline(sample: Path, tmp: Path) -> str:
    """captions.make_for_clip() end to end, with the speech recogniser faked (the model needs a download)."""
    saved = (captions.whisper_available, captions.transcribe_words, config.CAPTIONS)
    try:
        config.CAPTIONS = True
        captions.whisper_available = lambda: True
        words = [captions.Word(t, 0.5 + i * 0.4, 0.8 + i * 0.4) for i, t in enumerate("THAT WAS SO CLOSE".split())]
        captions.transcribe_words = lambda wav, upper=None: (_ for _ in ()).throw(AssertionError("no wav")) if not wav.exists() else words
        work = tmp / "cap-pipeline"
        ass = captions.make_for_clip(sample, 60.0, 8.0, work)
        assert ass and ass.name == "captions.ass" and "THAT" in ass.read_text(encoding="utf-8")
        assert (work / "fonts" / captions.FONT_FILE).exists() and not (work / "speech.wav").exists()
        captions.transcribe_words = lambda wav, upper=None: words[:2]          # barely any speech -> no captions
        assert captions.make_for_clip(sample, 60.0, 8.0, work) is None
        config.CAPTIONS = False
        assert captions.make_for_clip(sample, 60.0, 8.0, work) is None       # switched off
    finally:
        captions.whisper_available, captions.transcribe_words, config.CAPTIONS = saved
    return "audio extracted, subtitles written, font installed; no speech or switched off -> no captions"


def t_auto_update(tmp: Path) -> str:
    import main as pipeline

    state = State(tmp / "upd.db")
    calls: list[list[str]] = []
    real_run, real_env, real_flag = subprocess.run, os.environ.pop("GITHUB_ACTIONS", None), config.AUTO_UPDATE

    class Done:
        returncode, stderr = 0, ""

    try:
        config.AUTO_UPDATE = True
        subprocess.run = lambda cmd, **kw: (calls.append(list(cmd)), Done())[1]
        pipeline.maybe_update_ytdlp(state)
        assert len(calls) == 1 and calls[0][-1] == "yt-dlp[default]" and "-U" in calls[0], calls
        pipeline.maybe_update_ytdlp(state)                      # done a moment ago: not due for a week
        assert len(calls) == 1
        Done.returncode, Done.stderr = 1, "no network"
        state.set_meta("ytdlp_next_update", "2000-01-01T00:00:00+00:00")
        pipeline.maybe_update_ytdlp(state)                      # fails: retried later, not in a tight loop
        assert len(calls) == 2
        pipeline.maybe_update_ytdlp(state)
        assert len(calls) == 2
        config.AUTO_UPDATE = False
        state.set_meta("ytdlp_next_update", "2000-01-01T00:00:00+00:00")
        pipeline.maybe_update_ytdlp(state)
        assert len(calls) == 2                                  # switched off
    finally:
        subprocess.run, config.AUTO_UPDATE = real_run, real_flag
        if real_env is not None:
            os.environ["GITHUB_ACTIONS"] = real_env
    state.close()
    return "yt-dlp updates itself about weekly, retries failures after 6 hours"


def main() -> int:
    logging.disable(logging.CRITICAL)      # the flow test provokes expected warnings
    environment()
    tmp = Path(tempfile.mkdtemp(prefix="clipforge-selftest-"))
    try:
        check("RSS parsing", t_feed)
        check("first-run seeding / dedupe (SQLite)", lambda: t_seeding(tmp))
        check("channel links and @handles", t_resolver)
        check("channels.txt loading", lambda: t_channel_list(tmp))
        check("heatmap peak -> window", t_heatmap)
        check("comment timestamp parsing", t_timestamps)
        check("upload metadata", t_metadata)
        check("daily limits", lambda: t_caps(tmp))
        check("caption text -> timed subtitles", t_captions_text)
        check("title styles", t_titles)
        check("learning: finds the best options from views alone", lambda: t_bandit(tmp))
        check("learning: still explores when it knows nothing", lambda: t_exploring(tmp))
        check("learning: your own settings win", lambda: t_plan_overrides(tmp))
        check("learning: source channels and rising videos ranked", lambda: t_ranking(tmp))
        check("learning: mistakes and weak sources", lambda: t_mistakes(tmp))
        check("learning: measuring posted Shorts", lambda: t_tracking(tmp))
        check("learning: one request at most every 30 min", lambda: t_track_throttle(tmp))
        check("learning: report and best posting hour", lambda: t_hours_and_report(tmp))
        check("learning: clips stored with their choices", lambda: t_clip_record(tmp))
        check("run loop with fakes", lambda: t_flow(tmp))
        check("YouTube connection check", lambda: t_check_youtube(tmp))
        check("empty channel list is not a failure", lambda: t_no_channels(tmp))
        check("run-page messages", t_annotate)
        check("cookies secret", lambda: t_cookies(tmp))
        check("single-instance lock", lambda: t_lock(tmp))
        check("yt-dlp self-update", lambda: t_auto_update(tmp))

        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            sample = tmp / "sample.mp4"
            try:
                make_sample(sample)
            except Exception as exc:  # noqa: BLE001
                RESULTS.append(("FAIL", "generate synthetic video", str(exc)))
            else:
                check("audio-energy fallback finds the loud burst", lambda: t_audio(sample))
                check("render 1080x1920 (blur layout)", lambda: t_render(sample, tmp, "blur"))
                check("render 1080x1920 (crop layout)", lambda: t_render(sample, tmp, "crop"))
                check("full make_short() path", lambda: t_make_short(sample, tmp))
                if captions.filter_available():
                    check("captions burned into the video", lambda: t_caption_burn(sample, tmp))
                    check("caption pipeline (speech recogniser faked)", lambda: t_caption_pipeline(sample, tmp))
        else:
            warn("audio + render tests", "skipped: ffmpeg/ffprobe not available")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    width = max(len(name) for _, name, _ in RESULTS)
    for status, name, note in RESULTS:
        print(f"[{status}] {name.ljust(width)}  {note}")
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    print(f"\n{len(RESULTS) - len(failed)} ok/warn, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
