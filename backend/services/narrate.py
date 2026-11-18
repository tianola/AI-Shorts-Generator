"""Narrate Studio: a faceless Short from an idea - script, voice, footage, captions.

The product idea (script from a prompt, local neural/system voices, background footage,
word-by-word captions) follows Clipify AI's Narrate Studio
(github.com/sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE, CC BY-NC 4.0,
non-commercial); the implementation is our own and reuses our Whisper, caption and
LLM stack.

Voices: Windows SAPI (always there), Kokoro when the `kokoro` package is installed,
ElevenLabs when a key is configured (it is the one that speaks Greek well).
"""

import json
import os
import random
import re
import shutil
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
KOKORO_VOICES = ("af_heart", "af_bella", "af_nicole", "am_adam", "am_michael", "bf_emma", "bm_george")


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

def _powershell() -> str:
    return shutil.which("powershell.exe") or "powershell.exe"


def _sapi_voices() -> List[dict]:
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "[System.Speech.Synthesis.SpeechSynthesizer]::new().GetInstalledVoices() | "
        "ForEach-Object { $v=$_.VoiceInfo; [pscustomobject]@{name=$v.Name; lang=$v.Culture.Name} } "
        "| ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=30).stdout.strip()
        data = json.loads(out) if out else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"id": f"sapi:{v['name']}", "name": v["name"], "engine": "Windows", "language": v.get("lang", "")}
            for v in data if isinstance(v, dict) and v.get("name")]


def _kokoro_installed() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("kokoro") is not None
    except (ImportError, ValueError):
        return False


def list_voices() -> List[dict]:
    voices: List[dict] = []
    if settings.ELEVENLABS_API_KEY and settings.TTS_ENABLED:
        voices.append({"id": "elevenlabs:default", "name": "ElevenLabs (multilingual)",
                       "engine": "ElevenLabs", "language": "multi"})
    if _kokoro_installed():
        voices += [{"id": f"kokoro:{v}", "name": f"Kokoro {v[3:].title()}", "engine": "Kokoro",
                    "language": "en"} for v in KOKORO_VOICES]
    return voices + _sapi_voices()


def synthesize(text: str, voice: str, out_path: Path, rate: int = 0) -> Path:
    """Speak `text` into out_path (wav or mp3, by engine). Raises NarrateError."""
    text = re.sub(r"[\ud800-\udfff]", " ", text).strip()
    if not text:
        raise NarrateError("Nothing to say")
    engine, _, name = str(voice or "").partition(":")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if engine == "elevenlabs":
        from services.tts_elevenlabs import synthesize_to_file
        target = out_path.with_suffix(".mp3")
        if not synthesize_to_file(text, str(target)):
            raise NarrateError("ElevenLabs did not return audio")
        return target

    if engine == "kokoro":
        try:
            import numpy as np
            import soundfile as sf
            from kokoro import KPipeline
            pipeline = KPipeline(lang_code="b" if name.startswith("b") else "a")
            speed = max(0.8, min(1.15, 1.0 + rate * 0.04))
            chunks = [np.asarray(a.detach().cpu().numpy() if hasattr(a, "detach") else a)
                      for _, _, a in pipeline(text, voice=name, speed=speed)]
            if not chunks:
                raise NarrateError("Kokoro returned no audio")
            target = out_path.with_suffix(".wav")
            sf.write(str(target), np.concatenate(chunks), 24000)
            return target
        except ImportError as exc:
            raise NarrateError(f"Kokoro is not installed: {exc}")

    # Windows SAPI. The text goes through a file so quotes and newlines cannot break the script.
    target = out_path.with_suffix(".wav")
    text_file = out_path.with_suffix(".txt")
    text_file.write_text(text, encoding="utf-8")
    voice_line = f"$s.SelectVoice('{name.replace(chr(39), chr(39) * 2)}');" if name else ""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"{voice_line}$s.Rate={max(-10, min(10, int(rate)))};"
        f"$s.SetOutputToWaveFile('{str(target)}');"
        f"$s.Speak([IO.File]::ReadAllText('{str(text_file)}',[Text.Encoding]::UTF8));$s.Dispose()"
    )
    try:
        proc = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NarrateError(f"Windows speech could not start: {exc}")
    finally:
        try:
            text_file.unlink()
        except OSError:
            pass
    if proc.returncode or not target.is_file() or target.stat().st_size < 1024:
        raise NarrateError(f"Windows speech failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
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


def _word_timings(audio: Path, script: str, duration: float, job_dir: Path) -> Path:
    words_file = job_dir / "words.json"
    try:
        from services.whisper import transcribe_audio_file
        _segments, words_path = transcribe_audio_file(str(audio), str(job_dir), whisper_model="tiny")
        if words_path and os.path.isfile(words_path) and json.loads(Path(words_path).read_text(encoding="utf-8")):
            return Path(words_path)
    except Exception as exc:  # recognition is a nicety; the captions must still appear
        logger.warning(f"Narrate: word alignment failed ({type(exc).__name__}: {exc}); spreading words by length")
    words_file.write_text(json.dumps(even_words(script, duration)), encoding="utf-8")
    return words_file


def _ffmpeg(args: List[str], what: str) -> None:
    proc = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode:
        raise NarrateError(f"ffmpeg failed while {what}: {proc.stderr[-400:]}")


def render(job_dir: Path, script: str, voice: str, background: Optional[str] = None,
           rate: int = 0, caption_style: str = "mrbeast",
           progress: Callable[[str, int], None] = lambda _m, _p: None) -> Path:
    """Narrated Short -> job_dir/final.mp4. `background` is a library file name or None (random)."""
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

    progress("Preparing the footage", 60)
    base = job_dir / "base.mp4"
    clips = [safe_library_file(background)] if background else [
        library_dir() / item["name"] for item in list_library()]
    if clips:
        src = random.choice(clips)
        src_len = _duration(src)
        offset = random.uniform(0, max(0.0, src_len - length - 1)) if src_len > length + 1 else 0.0
        video_in = ["-stream_loop", "-1", "-ss", f"{offset:.2f}", "-i", str(src)]
    else:  # no library yet: a calm gradient so the tool still produces a video
        video_in = ["-f", "lavfi", "-i", f"gradients=s=1080x1920:d={length + 1:.2f}:speed=0.01:c0=0x1b1f3b:c1=0x7a3cff:rate=30"]
    _ffmpeg([*video_in, "-i", str(audio), "-t", f"{length:.2f}",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p",
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", settings.FFMPEG_PRESET,
             "-crf", str(settings.FFMPEG_CRF), "-c:a", "aac", "-b:a", "160k", "-shortest", str(base)],
            "building the background")

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
