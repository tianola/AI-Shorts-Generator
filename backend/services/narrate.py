"""Narrate Studio: a faceless Short from an idea - script, voice, footage, captions.

The product idea (script from a prompt, local neural/system voices, background footage,
word-by-word captions) follows Clipify AI's Narrate Studio
(github.com/sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE, CC BY-NC 4.0,
non-commercial); the implementation is our own and reuses our Whisper, caption and
LLM stack.

Voice: ElevenLabs only (ELEVENLABS_API_KEY). No system or local voices.
"""

import json
import os
import random
import re
import subprocess
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from loguru import logger

from config import settings

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
WORDS_PER_MINUTE = 150
SCRIPT_STYLES = {
    "story": "a gripping short story told in the first or third person, with a twist",
    "facts": "surprising facts, each one stronger than the last",
    "motivation": "a punchy motivational monologue",
    "horror": "a short, creepy story that builds dread",
    "explainer": "a clear explainer that answers one question",
}


class NarrateError(RuntimeError):
    pass


# ------------------------------------------------------------------ folders

def root() -> Path:
    path = Path(settings.STORAGE_ROOT) / "narrate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def library_dir() -> Path:
    path = root() / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_library() -> List[dict]:
    items = []
    for entry in sorted(library_dir().iterdir()):
        if entry.is_file() and entry.suffix.lower() in VIDEO_EXT:
            items.append({"name": entry.name, "size": entry.stat().st_size})
    return items


def safe_library_file(name: str) -> Path:
    """A library video by file name; anything with a path in it is refused."""
    if not name or name != os.path.basename(name) or name.startswith("."):
        raise NarrateError("Unknown background video")
    path = library_dir() / name
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
        raise NarrateError("Unknown background video")
    return path


# ------------------------------------------------------------------ script

def clean_script(raw: str) -> str:
    """Spoken text only: no headings, stage directions, markdown or scene labels."""
    text = re.sub(r"```.*?```", " ", str(raw or ""), flags=re.S)
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S | re.I)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or re.match(r"^(#{1,6}\s|[-*_]{3,}$)", line):
            continue
        line = line.replace("*", "").replace("_", " ")
        line = re.sub(r"^[\s>\-•]+", "", line)
        line = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", line).strip()   # [pause] (music)
        line = re.sub(r"^(title|scene\s*\d*|narrator|voice\s*over|voiceover|hook|intro|outro|script)"
                      r"\s*[:\-]\s*", "", line, flags=re.I)
        if line.strip():
            lines.append(line.strip())
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def fit_script_to_duration(script: str, seconds: int) -> str:
    """Whole sentences only, as many as fit the target length at a natural pace."""
    budget = max(10, int(seconds * WORDS_PER_MINUTE / 60))
    sentences = re.split(r"(?<=[.!?…])\s+", script.strip())
    kept, used = [], 0
    for sentence in sentences:
        count = len(sentence.split())
        if kept and used + count > budget:
            break
        kept.append(sentence)
        used += count
    return " ".join(kept).strip()


async def generate_script(idea: str, style: str = "story", seconds: int = 40,
                          language: str = "") -> str:
    """A narration script for `idea`, ready to be spoken (no headings or directions)."""
    from services.llm_router import call_llm_json

    idea = str(idea or "").strip()
    if not idea:
        raise NarrateError("Write an idea first")
    seconds = max(15, min(90, int(seconds)))
    words = int(seconds * WORDS_PER_MINUTE / 60)
    kind = SCRIPT_STYLES.get(style, SCRIPT_STYLES["story"])
    lang = f" Write it in {language}." if language else " Write it in the language of the idea."
    prompt = (
        f"Write the narration for a vertical short video: {kind}.\n"
        f"Idea: {idea}\n"
        f"About {words} words, spoken aloud in roughly {seconds} seconds.{lang}\n"
        "The first sentence must hook the viewer at once. Short, concrete sentences. "
        "End on a line that lands. Plain spoken text only: no title, no headings, no "
        "stage directions, no emojis, no hashtags.\n"
        'Return JSON only: {"script": "the full narration"}'
    )
    raw = await call_llm_json(
        prompt=prompt,
        system="You write tight, hook-first narration for short vertical videos. JSON only.",
        model=settings.OMNIROUTE_MODEL, temperature=0.8, max_tokens=2500,
    )
    try:
        text = json.loads(raw).get("script", "")
    except ValueError:
        text = raw
    script = fit_script_to_duration(clean_script(text), seconds + 8)
    if len(script.split()) < 8:
        raise NarrateError("The model did not return a usable script; try again")
    return script


# ------------------------------------------------------------------ voices

ELEVENLABS_VOICE = "elevenlabs:default"


def list_voices() -> List[dict]:
    """The voices offered in the UI: ElevenLabs only, and only when it is configured."""
    if settings.ELEVENLABS_API_KEY and settings.TTS_ENABLED:
        return [{"id": ELEVENLABS_VOICE, "name": "ElevenLabs", "engine": "ElevenLabs", "language": "multi"}]
    return []


def synthesize(text: str, voice: str, out_path: Path, rate: int = 0) -> Path:
    """Speak `text` with ElevenLabs into out_path (mp3). Raises NarrateError."""
    text = text.encode("utf-8", "replace").decode("utf-8").strip()   # drop lone surrogates
    if not text:
        raise NarrateError("Nothing to say")
    if not (settings.ELEVENLABS_API_KEY and settings.TTS_ENABLED):
        raise NarrateError("ElevenLabs is not configured: set ELEVENLABS_API_KEY in backend/.env")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from services.tts_elevenlabs import synthesize_to_file
    target = out_path.with_suffix(".mp3")
    if not synthesize_to_file(text, str(target)):
        raise NarrateError("ElevenLabs did not return audio")
    return target


# ------------------------------------------------------------------ render

def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        raise NarrateError(f"Cannot read the length of {path.name}")


def even_words(script: str, duration: float) -> List[dict]:
    """Word timings spread by length, used when speech recognition cannot align the voice."""
    tokens = script.split()
    total = sum(len(t) + 1 for t in tokens) or 1
    out, t = [], 0.0
    for token in tokens:
        span = duration * (len(token) + 1) / total
        out.append({"word": " " + token, "start": round(t, 3), "end": round(t + span * 0.92, 3)})
        t += span
    return out


def _norm(token: str) -> str:
    return re.sub(r"[\W_]+", "", token.lower())


def align_script_to_words(script: str, heard: List[dict], duration: float) -> List[dict]:
    """The script's own words, timed by what speech recognition heard.

    Recognition mishears ("a dish" -> "addition") and drops short words, so its text is
    not used for the captions: the script is exactly what was spoken. Words that match
    take the recognised timing; each run of unmatched words is spread by length between
    the nearest matched neighbours, so there is never a stretch of speech without a caption.
    """
    import difflib

    tokens = script.split()
    if not tokens:
        return []
    # A recognised word with no length is a timing the recogniser made up; it is not an anchor.
    heard = [w for w in heard if _norm(str(w.get("word", ""))) and float(w["end"]) - float(w["start"]) >= 0.05]
    a = [_norm(t) for t in tokens]
    b = [_norm(str(w["word"])) for w in heard]
    timing: List[Optional[tuple]] = [None] * len(tokens)
    for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            src = heard[block.b + k]
            timing[block.a + k] = (float(src["start"]), float(src["end"]))

    out: List[dict] = []
    i = 0
    cursor = 0.0
    while i < len(tokens):
        if timing[i] is not None:
            start, end = timing[i]
            start = max(start, cursor)
            end = max(end, start + 0.12)
            out.append({"word": " " + tokens[i], "start": round(start, 3), "end": round(end, 3)})
            cursor = end
            i += 1
            continue
        j = i
        while j < len(tokens) and timing[j] is None:
            j += 1
        right = timing[j][0] if j < len(tokens) else duration
        right = max(right, cursor + 0.12 * (j - i))
        weights = [len(t) + 1 for t in tokens[i:j]]
        span = (right - cursor) / sum(weights)
        for token, weight in zip(tokens[i:j], weights):
            out.append({"word": " " + token, "start": round(cursor, 3),
                        "end": round(cursor + span * weight * 0.92, 3)})
            cursor += span * weight
        i = j
    return out


def _word_timings(audio: Path, script: str, duration: float, job_dir: Path) -> Path:
    words_file = job_dir / "words.json"
    try:
        from services.whisper import transcribe_audio_file
        _segments, words_path = transcribe_audio_file(str(audio), str(job_dir), whisper_model="tiny")
        if words_path and os.path.isfile(words_path):
            heard = json.loads(Path(words_path).read_text(encoding="utf-8"))
            if heard:
                words_file.write_text(json.dumps(align_script_to_words(script, heard, duration)),
                                      encoding="utf-8")
                return words_file
    except Exception as exc:  # recognition is a nicety; the captions must still appear
        logger.warning(f"Narrate: word alignment failed ({type(exc).__name__}: {exc}); spreading words by length")
    words_file.write_text(json.dumps(even_words(script, duration)), encoding="utf-8")
    return words_file


def _ffmpeg(args: List[str], what: str) -> None:
    proc = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode:
        raise NarrateError(f"ffmpeg failed while {what}: {proc.stderr[-400:]}")


def _pick_sources(script: str, background: Optional[str]) -> List[Path]:
    """The footage to use: the chosen library file, else stock found for this script, else
    whatever is in the library, else nothing (a plain gradient is drawn)."""
    if background:
        return [safe_library_file(background)]
    try:
        import asyncio
        from services import stock_footage
        found = asyncio.run(stock_footage.footage_for(script, library_dir()))
    except Exception as exc:  # no key, no network: carry on with what we have
        logger.warning(f"Narrate: no stock footage ({type(exc).__name__}: {exc})")
        found = []
    if found:
        return found
    own = [library_dir() / item["name"] for item in list_library()]
    return random.sample(own, min(3, len(own))) if own else []


def _build_background(sources: List[Path], length: float, job_dir: Path) -> Path:
    """A 1080x1920 30 fps video of exactly `length` seconds, cut from the sources in turn
    (each looped if shorter than its share), or a gradient when there are none."""
    out = job_dir / "background.mp4"
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p"
    enc = ["-an", "-c:v", "libx264", "-preset", settings.FFMPEG_PRESET, "-crf", str(settings.FFMPEG_CRF)]
    if not sources:
        _ffmpeg(["-f", "lavfi", "-i", f"gradients=s=1080x1920:d={length + 1:.2f}:speed=0.01:c0=0x1b1f3b:c1=0x7a3cff:rate=30",
                 "-t", f"{length:.2f}", *enc, str(out)], "drawing the gradient")
        return out
    share = length / len(sources)
    parts = []
    for i, src in enumerate(sources):
        src_len = _duration(src)
        offset = random.uniform(0, max(0.0, src_len - share - 1)) if src_len > share + 1 else 0.0
        part = job_dir / f"bg_{i}.mp4"
        _ffmpeg(["-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(src), "-t", f"{share:.2f}",
                 "-vf", vf, *enc, str(part)], f"cutting footage {i + 1}")
        parts.append(part)
    if len(parts) == 1:
        return parts[0]
    listing = job_dir / "bg_parts.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)], "joining the footage")
    return out


def render(job_dir: Path, script: str, voice: str, background: Optional[str] = None,
           rate: int = 0, caption_style: str = "mrbeast",
           progress: Callable[[str, int], None] = lambda _m, _p: None) -> Path:
    """Narrated Short -> job_dir/final.mp4. `background` is a library file name, or None to find stock footage that fits the script."""
    job_dir.mkdir(parents=True, exist_ok=True)
    script = clean_script(script)
    if len(script.split()) < 3:
        raise NarrateError("The script is empty")

    progress("Recording the voice", 10)
    audio = synthesize(script, voice, job_dir / "voice", rate)
    length = _duration(audio) + 0.4
    if length > 180:
        raise NarrateError("The narration is longer than 3 minutes; shorten the script")

    progress("Syncing the words", 35)
    words_file = _word_timings(audio, script, length, job_dir)

    progress("Styling captions", 50)
    from services.captions import create_captions
    captions = job_dir / "captions.ass"
    create_captions(str(words_file), str(captions), style_name=caption_style)

    progress("Finding footage", 55)
    sources = _pick_sources(script, background)
    progress("Preparing the footage", 62)
    base = job_dir / "base.mp4"
    video_only = _build_background(sources, length, job_dir)
    _ffmpeg(["-i", str(video_only), "-i", str(audio), "-t", f"{length:.2f}", "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(base)],
            "adding the voice")

    progress("Burning captions", 80)
    from services.render import burn_captions
    final = job_dir / "final.mp4"
    burn_captions(str(base), str(captions), str(final))
    if not final.is_file() or final.stat().st_size < 1024:
        raise NarrateError("The final video was not written")
    progress("Done", 100)
    return final


def new_job_id() -> str:
    return uuid.uuid4().hex[:10]
