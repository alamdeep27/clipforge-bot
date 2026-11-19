"""captions.py - free automatic captions burned into the Short.

Speech -> text runs on the CPU with faster-whisper (no account, no API key, no cost).
The words become an ASS subtitle file (3 words at a time, the spoken word highlighted
in yellow) that ffmpeg burns into the video. If anything is missing or no speech is
found, the Short is simply made without captions.
"""
from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import config

log = logging.getLogger(__name__)

FONT_NAME = "Poppins"
FONT_FILE = "Poppins-Bold.ttf"
BASE_COLOUR = "&HFFFFFF&"       # white
ACTIVE_COLOUR = "&H00FFFF&"     # yellow (ASS colours are blue-green-red)
_STRIP = " ,.;:\u2026\"'\u201c\u201d\u2018\u2019()[]{}<>-\u2014*_"
_filter_ok: bool | None = None
_model = None


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


# --------------------------------------------------------------------------
# Capability checks
# --------------------------------------------------------------------------
def filter_available() -> bool:
    """Does this ffmpeg have the `subtitles` filter (libass)?"""
    global _filter_ok
    if _filter_ok is None:
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, errors="replace", timeout=60
            ).stdout
            _filter_ok = any(line.split()[1:2] == ["subtitles"] for line in out.splitlines())
        except (OSError, subprocess.SubprocessError):
            _filter_ok = False
    return _filter_ok


def whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


# --------------------------------------------------------------------------
# Words -> ASS
# --------------------------------------------------------------------------
def clean_word(raw: str, upper: bool | None = None) -> str:
    text = (raw or "").replace("\\", "").replace("{", "").replace("}", "").strip().strip(_STRIP)
    upper = config.CAPTION_UPPER if upper is None else upper
    return text.upper() if upper else text


def chunk_words(words: list[Word], max_words: int | None = None, max_chars: int = 16, max_gap: float = 0.7):
    """Group words into short on-screen lines."""
    max_words = max_words or config.CAPTION_WORDS
    chunks: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if current:
            length = sum(len(w.text) + 1 for w in current) + len(word.text)
            if len(current) >= max_words or length > max_chars or word.start - current[-1].end > max_gap:
                chunks.append(current)
                current = []
        current.append(word)
    if current:
        chunks.append(current)
    return chunks


def ass_time(seconds: float) -> str:
    centis = int(round(max(0.0, seconds) * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def build_ass(words: list[Word], words_per_line: int | None = None) -> str:
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{FONT_NAME},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,3,2,70,70,360,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    chunks = chunk_words(words, max_words=words_per_line)
    events: list[str] = []
    for ci, chunk in enumerate(chunks):
        next_chunk_start = chunks[ci + 1][0].start if ci + 1 < len(chunks) else None
        for wi, word in enumerate(chunk):
            start = word.start
            if wi + 1 < len(chunk):
                end = chunk[wi + 1].start
            else:
                end = max(word.end, word.start + 0.25)
                if next_chunk_start is not None:
                    end = min(end, next_chunk_start)
            end = max(end, start + 0.12)
            text = " ".join(
                f"{{\\c{ACTIVE_COLOUR}}}{w.text}{{\\c{BASE_COLOUR}}}" if j == wi else w.text
                for j, w in enumerate(chunk)
            )
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


# --------------------------------------------------------------------------
# Speech -> words
# --------------------------------------------------------------------------
def extract_wav(src: Path, start: float, duration: float, out: Path) -> Path:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"could not extract audio for captions: {proc.stderr.strip()[-200:]}")
    return out


def _load_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        log.info("loading the caption model '%s' (first time downloads it)", config.WHISPER_MODEL)
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def transcribe_words(wav: Path, upper: bool | None = None) -> list[Word]:
    model = _load_model()
    segments, _info = model.transcribe(
        str(wav),
        word_timestamps=True,
        vad_filter=True,
        beam_size=1,
        condition_on_previous_text=False,
        language=config.CAPTION_LANGUAGE,
    )
    words: list[Word] = []
    for segment in segments:
        # skip stretches that are probably game noise or music that the model "heard" as speech
        if getattr(segment, "no_speech_prob", 0.0) > 0.6 or getattr(segment, "avg_logprob", 0.0) < -1.5:
            continue
        for w in segment.words or []:
            text = clean_word(w.word, upper)
            if text:
                words.append(Word(text, float(w.start), float(w.end)))
    return words


def install_font(work_dir: Path) -> None:
    fonts = work_dir / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    bundled = config.BASE_DIR / "fonts" / FONT_FILE
    if bundled.exists():
        shutil.copy(bundled, fonts / FONT_FILE)
    else:
        log.warning("fonts/%s is missing; captions will use a system font", FONT_FILE)


def make_for_clip(
    src: Path, offset: float, duration: float, work_dir: Path,
    words_per_line: int | None = None, upper: bool | None = None,
) -> Path | None:
    """Create work_dir/captions.ass for the clip [offset, offset+duration] of `src`, or return None."""
    if not config.CAPTIONS:
        return None
    if not filter_available():
        log.warning("captions skipped: this ffmpeg has no 'subtitles' filter (needs libass)")
        return None
    if not whisper_available():
        log.warning("captions skipped: faster-whisper is not installed (pip install -r requirements.txt)")
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / "speech.wav"
    try:
        extract_wav(src, offset, duration, wav)
        words = transcribe_words(wav, upper)
    finally:
        wav.unlink(missing_ok=True)
    if len(words) < 3:
        log.info("no clear speech in this clip; making it without captions")
        return None
    ass = work_dir / "captions.ass"
    ass.write_text(build_ass(words, words_per_line), encoding="utf-8")
    install_font(work_dir)
    log.info("captions ready: %d words", len(words))
    return ass
