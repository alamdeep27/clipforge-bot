# ClipForge Shorts bot - technical reference

New here? Read **START-HERE.md** first. This page is the reference.

Polls the YouTube channels in `channels.txt` through their free RSS feeds, finds the most-replayed (or loudest) moment of every new upload, turns it into a captioned 1080x1920 Short with ffmpeg, and either saves it ready to post (default) or uploads it with the YouTube Data API.

```
channels.txt -> RSS poll -> new video? -> hottest moment -> captions + ffmpeg 9:16 -> delivery
 (up to 500)    (no quota)   (SQLite)     (heatmap|audio)    (<59 s, loudnorm)        files | api
```

## Things that decide how this works (verified against current docs)

1. **Auto-posting needs Google's approval.** Videos uploaded through `videos.insert` by an unaudited API project are locked private, and Google's help says the owner cannot appeal or unlock them. Hence the default `YT_DELIVERY=files`. Docs: https://developers.google.com/youtube/v3/docs/videos/insert
2. **Upload quota.** `videos.insert` has its own default bucket of 100 calls/day at 1 unit each (https://developers.google.com/youtube/v3/determine_quota_cost). In api mode the bot stops at 90 (`YT_MAX_UPLOADS_PER_DAY`), counting days in Pacific time, and stops for the day if Google answers `quotaExceeded`.
3. **Cloud servers are often blocked by YouTube** for yt-dlp (datacenter IP addresses). `python main.py --check-youtube` tests this on any computer; the GitHub workflow has a "check only" button for it. Your own PC is the reliable host.
4. **yt-dlp needs a JavaScript runtime** (Deno) and a current version. The bot updates yt-dlp itself about weekly (`YT_AUTO_UPDATE=0` to stop that). With Node instead of Deno, set `YT_JS_RUNTIMES=node`. https://github.com/yt-dlp/yt-dlp/wiki/EJS
5. **The heatmap is empty for new uploads**, so the audio-energy fallback (loudness spikes, boosted by comment timestamps and live-chat density when available) is what usually picks the moment. It finds loud hype, not quiet clutch plays.

## The channel list

`channels.txt`: one channel per line, as a channel link (`https://www.youtube.com/@name`), an `@name`, or the 24-character `UC...` id. `#` starts a note or comments out a line. Handles are resolved to ids once and remembered. **Everything listed is treated as approved by you**: only add channels whose owners said yes. Up to 500 channels (`MAX_CHANNELS` in `config.py`). An older `channels.json` (`[{"id": "UC...", "name": "..."}]`) still works if you point `YT_CHANNELS_FILE` at it.

## Learning from results (`learner.py`)

Each Short is stored with its choices (layout, length, caption style, title style, source channel). The bot reads your channel's public RSS feed (the feed lists views and likes and the full description) every 30 minutes at most, links each posted Short back to its clip through the source link in the description (title as fallback), and saves views at 6/24/72 hours. Score = `log10(1 + views at 24 h)`. Decisions use Thompson sampling: each option gets a plausible score drawn from what is known (wide when data is thin, narrow when it is plentiful) and the best draw wins, so it explores early and settles on winners later. Source videos are ranked by the channel's learned score plus the source video's own views per hour. Channels whose last 4 videos all failed are paused for 7 days; channels with at least 6 measured Shorts far below your average (about 4x fewer views) are paused for 14 days. Everything it changes is written to a diary: `python main.py --report` (also saved as `LEARNING-REPORT.md`, also shown in the GitHub run summary). Anything you set yourself (`YT_LAYOUT`, `YT_CLIP_SECONDS`, `YT_CAPTION_WORDS`, `YT_CAPTION_UPPER`, `--layout`) is never overridden; `YT_LEARNING=0` switches learning off.

Limits: the feed shows your 15 newest videos, so measurement works up to roughly 10 Shorts a day; videos locked private (unaudited API uploads) have no public numbers; views are noisy and about 30 measured Shorts are needed before patterns mean much.

## Commands

```bash
python main.py                          # normal run: poll, clip, deliver
python main.py --poll-only              # only look for new uploads (stdlib only, a few seconds)
python main.py --process-only           # clip what is waiting
python main.py --seed-only              # mark what exists now as seen
python main.py --dry-run                # list what WOULD be picked up; changes nothing
python main.py --dry-run --video LINK   # one test Short from one video -> output/dry-run/
python main.py --dry-run --local-file F # same from a file on disk, no internet
python main.py --check-youtube          # can THIS computer run the bot?
python main.py --status                 # counters and today's budget
python main.py --report                 # what the bot has learned from your Shorts' views
python main.py --auth                   # one-time Google login (api delivery only)
python selftest.py                      # offline checks of everything except live YouTube/speech/upload
```
On the first poll of any channel (also channels you add later) its existing videos are marked as seen, so only new uploads are ever clipped. Uploads older than `YT_MAX_VIDEO_AGE_HOURS` (72) are ignored.

## Mac / Linux install

```bash
# Ubuntu/Debian:  sudo apt install -y ffmpeg curl unzip && curl -fsSL https://deno.land/install.sh | sh
# macOS:          brew install ffmpeg deno
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python selftest.py
```
Schedule it with `crontab -e` (cron has a minimal PATH, so list the folders of `ffmpeg` and `deno`):
```cron
PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/home/you/.deno/bin
*/15 * * * * cd /home/you/clipforge-bot && ./.venv/bin/python main.py >> cron.log 2>&1
```

## Cloud (GitHub Actions)

`.github/workflows/shorts.yml` runs about every 15 minutes. A fast first step (`--poll-only`, standard library only, no installs) checks for new uploads; only when there is something to clip does it install Python packages, ffmpeg and Deno, make the Shorts, attach them to a GitHub Release and delete releases older than 7 days. The bot's memory (`state/processed_videos.db`) lives in GitHub's cache; if it is ever lost the bot simply re-seeds and does not re-clip old videos. Public repositories get unlimited free Actions minutes on standard runners; private free accounts get 2,000 minutes a month, so use an hourly schedule there.

Optional repository secrets/variables (Settings > Secrets and variables > Actions): secret `YT_COOKIES` (Netscape cookies text, for the "sign in to confirm you're not a bot" case; use a throwaway Google account), variable `YT_DELIVERY` (`files` or `api`), variable `YT_PRIVACY`, secrets `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` (api delivery). Keep the cloud and a home PC from running at the same time.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `YT_DELIVERY` | `files` | `files` = save finished Shorts + a title/description text; `api` = upload by itself |
| `YT_MAX_CLIPS_PER_DAY` | `12` | daily limit in files mode |
| `YT_MAX_UPLOADS_PER_DAY` | `90` | daily limit in api mode |
| `YT_PRIVACY` | `private` | api mode: `private`, `unlisted` or `public` (public only sticks after Google's audit) |
| `YT_MAX_PER_RUN` | `3` | videos processed per run |
| `YT_MAX_VIDEO_AGE_HOURS` | `72` | ignore uploads older than this (also the catch-up window after downtime) |
| `YT_CLIP_SECONDS` | `55` | target clip length (clamped to 45-58 s) |
| `YT_LAYOUT` | `blur` | `blur` = full frame over a blurred background, `crop` = centred 9:16 slice |
| `YT_CAPTIONS` | `1` | `0` = no captions |
| `YT_WHISPER_MODEL` | `base` | `tiny` (fastest), `base`, `small` (best, slowest) |
| `YT_CAPTION_LANGUAGE` | auto | e.g. `en` to skip language detection |
| `YT_CAPTION_UPPER` / `YT_CAPTION_WORDS` | `1` / `3` | UPPERCASE captions; words per line |
| `YT_TARGET_LUFS` | `-14` | loudness target |
| `YT_X264_PRESET` | `medium` | `veryfast` renders quicker on small machines |
| `YT_READY_KEEP_DAYS` | `14` | finished files older than this are deleted |
| `YT_AUTO_UPDATE` | `1` | weekly yt-dlp self-update (skipped in the cloud) |
| `YT_LEARNING` | `1` | `0` = no learning (fixed layout, length, captions, titles; FIFO order) |
| `YT_MY_CHANNEL_ID` | your ClipForge id | the channel whose public feed is read for results |
| `YT_REPORT_FILE` | `LEARNING-REPORT.md` | where `--report` saves the report |
| `YT_COOKIES_FILE` / `YT_COOKIES_TEXT` | none | cookies for yt-dlp |
| `YT_JS_RUNTIMES` | none | e.g. `node` |
| `YT_MY_CHANNEL_NAME` / `YT_MY_CHANNEL_URL` | ClipForge / https://www.youtube.com/@ClipForge-n3b | the "Subscribe" line in every description |
| `YT_CHANNELS_FILE`, `YT_DB_PATH`, `YT_TMP_DIR`, `YT_OUTPUT_DIR`, `YT_READY_DIR`, `YT_CLIENT_SECRET`, `YT_TOKEN_FILE` | next to `main.py` | file locations |

## Files

| File | Job |
|---|---|
| `main.py` | run loop, all CLI modes, lock file, per-video status (`seeded`, `new`, `ready`, `uploaded`, `skipped`, `failed`; three attempts before `failed`; a YouTube block keeps videos queued instead of failing them) |
| `permissions.py`, `resolve.py` | `channels.txt` loading; links and @handles to channel ids (no API key) |
| `feeds.py` | RSS polling with `urllib` + `xml.etree`, 12 feeds in parallel |
| `db.py` | SQLite `processed_videos.db`: seen videos, deliveries, daily counters |
| `detector.py` | `info["heatmap"]` peak, else audio RMS (+ comment timestamps, + live-chat replay) -> a 45-58 s window centred on the peak, skipping the first 15 s and last 10 s |
| `captions.py` | faster-whisper (CPU, free) -> word-timed ASS subtitles: 3 words per line, the spoken word in yellow, Poppins Bold (`fonts/`, OFL) |
| `clipper.py` | `yt-dlp --download-sections` for only that window, then ffmpeg: layout, burned-in captions, two-pass `loudnorm`, H.264/AAC, always under 59 s; raw files deleted immediately |
| `learner.py` | the learning loop: stores choices, measures views, picks options (Thompson sampling), ranks source videos, pauses failing/weak channels, writes the report |
| `candidates-top100-gaming.txt` | vidIQ's top-100 gaming channels as links to ASK for permission; not read by the bot |
| `delivery.py` | files mode: move the Short to `output/ready/` with a `.txt` (title, description, tags) |
| `uploader.py` | api mode: OAuth (`token.json` or cloud secrets), resumable `videos().insert()`, `#Shorts`, category 20, daily cap guard |
| `selftest.py` | offline checks of everything except live YouTube calls |

## Status of this build

Verified in an offline test environment (`python selftest.py`, all passing): RSS parsing (including views, likes and descriptions), the learning loop (a simulation with hidden "true" best options: the bot found them from views alone; exploration with no data; ranking; pauses; matching and scoring posted Shorts; the report), first-run seeding and de-duplication, channel links and @handles, heatmap and audio-energy detection, 1080x1920 rendering in both layouts with loudness near -14 LUFS, caption timing and burn-in (checked visually and by pixel difference), the caption pipeline with a faked recogniser, finished-file and upload delivery with fakes, daily limits, retries, the "YouTube is blocking us" path, the single-instance lock, the weekly yt-dlp update, and the GitHub workflow file's structure. **Not verifiable offline**, so run them once yourself: live yt-dlp downloads (`--check-youtube`, `--dry-run --video LINK`), real speech recognition (needs the model download, done by `SETUP-WINDOWS.bat`), handle lookups against live YouTube pages, the Windows setup scripts, the GitHub workflow run, and the Google upload.
