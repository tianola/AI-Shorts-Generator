"""
Service for audio transcription using Groq Whisper Turbo by default,
with local faster-whisper as a resilient fallback.

Supports:
- Auto language detection (ISO-639-1 code + confidence) via faster-whisper.
- Optional translation to English (local `task="translate"` or Groq
  `/audio/translations` endpoint).
- Optional explicit `language` override that pins the transcription language.

The public entry point `transcribe_audio_file()` remains backward compatible:
new parameters (`language`, `translate`) are optional and default to the old
behavior (auto-detect language, transcribe, never translate).
"""
import json
import os
import subprocess
from typing import List, Optional, Tuple, Callable

import httpx
from loguru import logger

from faster_whisper import WhisperModel
from config import settings

# Global model instance (loaded once, reused across jobs)
_model: Optional[WhisperModel] = None
_model_size: Optional[str] = None


def _approximate_words_from_text(text: str, start: float, end: float) -> List[dict]:
    cleaned = [word.strip() for word in text.split() if word.strip()]
    if not cleaned:
        return []

    total_duration = max(end - start, 0.1)
    words: List[dict] = []
    step = total_duration / len(cleaned)
    cursor = start
    for word in cleaned:
        word_start = cursor
        word_end = min(cursor + step, end)
        words.append({
            "word": word,
            "start": word_start,
            "end": word_end,
        })
        cursor = word_end
    return words


def get_whisper_model(model_size: Optional[str] = None) -> WhisperModel:
    global _model, _model_size

    if not model_size:
        model_size = settings.WHISPER_MODEL_SIZE

    if _model is None or _model_size != model_size:
        logger.info(f"Loading faster-whisper model: {model_size}")
        try:
            _model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _model_size = model_size
        except Exception as exc:
            logger.warning(f"Failed to load '{model_size}' ({exc}), falling back to 'tiny'")
            _model = WhisperModel("tiny", device="cpu", compute_type="int8")
            # Cache the size actually in memory. Caching the *requested* size
            # instead made every later call believe a large model was loaded
            # while the tiny fallback did all the work.
            _model_size = "tiny"
        logger.info("Whisper model loaded")

    return _model


def _write_transcript_metadata(output_dir: str, language: Optional[str],
                               language_confidence: Optional[float]) -> str:
    """Write detected language metadata alongside segments/words (additive)."""
    meta_file = os.path.join(output_dir, "transcript_info.json")
    meta = {
        "detected_language": language,
        "language_code": language,
        "language_confidence": language_confidence,
    }
    if language is None:
        meta["detected_language"] = None
        meta["language_confidence"] = None
    meta["detected"] = language is not None and language_confidence is not None
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta_file


def detect_audio_language(audio_path: str, model_size: Optional[str] = None,
                          language_detection_threshold: float = 0.5) -> Tuple[Optional[str], Optional[float]]:
    """Detect the spoken language of an audio file.

    Uses faster-whisper's `detect_language()` on the first 30s window (aligned
    with the library's default language_detection_segments=1). Returns an
    ISO-639-1 code and confidence, or (None, None) for silent/empty audio or
    when detection confidence is below the threshold.

    Args:
        audio_path: Path to an audio/video file.
        model_size: faster-whisper model size override (default from settings).
        language_detection_threshold: Minimum top-language probability to accept.

    Returns:
        Tuple of (language_code, confidence). Both None when no speech is
        detected or the audio cannot be decoded.
    """
    from faster_whisper.audio import decode_audio

    model = get_whisper_model(model_size)
    try:
        audio = decode_audio(audio_path, sampling_rate=16000)
    except Exception as exc:
        logger.warning(f"Could not decode audio for language detection: {exc}")
        return None, None

    if audio is None or len(audio) == 0:
        logger.info("Empty audio — no language detected")
        return None, None

    # Trim to a reasonable detection window (~30s) if the file is longer.
    max_samples = int(30.0 * 16000)
    if len(audio) > max_samples:
        audio = audio[:max_samples]

    try:
        language, confidence, _ = model.detect_language(
            audio,
            vad_filter=True,
            language_detection_threshold=language_detection_threshold,
        )
    except Exception as exc:
        logger.warning(f"Language detection failed: {exc}")
        return None, None

    if language is None or confidence is None:
        return None, None
    if confidence < language_detection_threshold:
        logger.warning(
            f"Language detection confidence too low ({confidence:.3f}) — "
            "leaving language undetected"
        )
        return None, None

    return language, float(confidence)


def _transcript_already_written(output_dir: str) -> bool:
    """True when a non-empty words.json from an earlier attempt is on disk."""
    words_file = os.path.join(output_dir, "words.json")
    if not os.path.isfile(words_file):
        return False
    try:
        with open(words_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, list) and len(data) > 0


def _write_segments_and_words(output_dir: str, segments_list: List[dict],
                              words_list: List[dict]) -> Tuple[str, str]:
    """Persist segments.json and words.json, returning their paths."""
    segments_file = os.path.join(output_dir, "segments.json")
    with open(segments_file, "w", encoding="utf-8") as f:
        json.dump(segments_list, f, ensure_ascii=False, indent=2)

    words_file = os.path.join(output_dir, "words.json")
    with open(words_file, "w", encoding="utf-8") as f:
        json.dump(words_list, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Transcription saved to {segments_file} and {words_file} "
        f"({len(segments_list)} segments, {len(words_list)} words)"
    )
    return segments_file, words_file


def _transcribe_with_local_whisper(
    audio_path: str,
    output_dir: str,
    model_size: Optional[str] = None,
    language: Optional[str] = None,
    translate: bool = False,
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> Tuple[str, str]:
    model = get_whisper_model(model_size)
    task = "translate" if translate else "transcribe"

    # Normalize: an explicit override is a string; auto-detection returns
    # a (code, confidence) tuple. Never index into a string by accident.
    language_code = language
    language_confidence = None
    if not language:
        detected = detect_audio_language(audio_path, model_size)
        if detected and detected[0]:
            language_code, language_confidence = detected
    elif isinstance(language, str):
        language_code = language
    elif isinstance(language, (tuple, list)) and len(language) >= 2 and language[0]:
        language_code, language_confidence = language[0], language[1]

    logger.info(
        f"Local whisper transcription (task={task}, language={language_code or 'auto'}, "
        f"confidence={language_confidence if language_confidence is not None else 'n/a'})"
    )
    segments, info = model.transcribe(
        audio_path,
        word_timestamps=True,
        vad_filter=True,
        language=language_code,
        task=task,
        beam_size=1,
    )

    total_dur = getattr(info, "duration", 0) or 1.0
    segments_list = []
    words_list = []

    for segment in segments:
        segment_data = {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        }
        segments_list.append(segment_data)

        if segment.words:
            for word in segment.words:
                words_list.append({
                    "word": word.word.strip(),
                    "start": word.start,
                    "end": word.end,
                })
        else:
            words_list.extend(_approximate_words_from_text(segment.text, segment.start, segment.end))

        if progress_callback and total_dur > 0:
            w_pct = min(99, int((segment.end / total_dur) * 100))
            overall_pct = int(25 + (w_pct * 0.25))
            progress_callback(f"Transcribing audio ({w_pct}%)", overall_pct)

    if progress_callback:
        progress_callback("Audio transcribed (100%)", 50)

    segments_file, words_file = _write_segments_and_words(output_dir, segments_list, words_list)
    _write_transcript_metadata(output_dir, language_code, language_confidence)
    return segments_file, words_file


def _compress_audio_for_groq(audio_path: str) -> str:
    """Compress audio to a small mp3 suitable for Groq's upload limit (25 MB)."""
    compressed = audio_path.rsplit(".", 1)[0] + "_groq.mp3"
    if os.path.exists(compressed) and os.path.getsize(compressed) < 24 * 1024 * 1024:
        return compressed
    # Use 48k bitrate to stay well under 25 MB even for 60-minute files
    cmd = [
        "ffmpeg", "-y", "-i", audio_path,
        "-ac", "1", "-ar", "16000", "-b:a", "48k",
        "-acodec", "libmp3lame",
        compressed,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return compressed


def _groq_language_from_payload(payload: dict) -> Tuple[Optional[str], Optional[float]]:
    """Best-effort language metadata from a Groq response payload."""
    lang = payload.get("language")
    if not lang:
        return None, None
    try:
        return str(lang), float(payload.get("language_probability") or 0.0)
    except (TypeError, ValueError):
        return str(lang), None


def _transcribe_with_groq(
    audio_path: str,
    output_dir: str,
    language: Optional[str] = None,
    translate: bool = False,
) -> Tuple[str, str]:
    api_key = settings.GROQ_API_KEY
    if not api_key:
        raise ValueError("GROQ_API_KEY is not configured")

    # Compress audio to stay under Groq's size limit
    groq_audio = _compress_audio_for_groq(audio_path)

    # Pre-upload size guard — skip Groq if still too large
    groq_size_mb = os.path.getsize(groq_audio) / (1024 * 1024)
    if groq_size_mb > 22:
        raise RuntimeError(
            f"Compressed audio ({groq_size_mb:.1f}MB) exceeds Groq 25MB limit — skipping to local"
        )

    # Detect language with the local model unless overridden. This keeps
    # response metadata populated even when the Groq payload omits it.
    detected = None
    if not language:
        detected = detect_audio_language(audio_path)
        if detected and detected[0]:
            language = detected[0]

    is_translation = bool(translate)
    endpoint = "translations" if is_translation else "transcriptions"

    with open(groq_audio, "rb") as f:
        files = {"file": (os.path.basename(groq_audio), f, "application/octet-stream")}
        data = {
            "model": settings.GROQ_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word", "segment"],
        }
        if language and not is_translation:
            data["language"] = language
        headers = {"Authorization": f"Bearer {api_key}"}

        with httpx.Client(timeout=300.0) as client:
            response = client.post(
                f"{settings.GROQ_BASE_URL.rstrip('/')}/audio/{endpoint}",
                files=files,
                data=data,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()

    if is_translation:
        # The translations endpoint returns plain text (no segments/words).
        transcript_text = str(payload.get("text") or "").strip()
        duration = float(payload.get("duration") or 0.0) or 1.0
        segments_list = []
        words_list = []
        if transcript_text:
            segments_list = [{"start": 0.0, "end": duration, "text": transcript_text}]
            words_list = _approximate_words_from_text(transcript_text, 0.0, duration)
    else:
        segments_payload = payload.get("segments") or []
        segments_list = []
        words_list = []

        if segments_payload:
            for segment in segments_payload:
                start = float(segment.get("start", 0.0) or 0.0)
                end = float(segment.get("end", start) or start)
                text = str(segment.get("text") or "").strip()
                segments_list.append({"start": start, "end": end, "text": text})

                words = segment.get("words") or []
                if words:
                    for word in words:
                        word_text = str(word.get("word") or "").strip()
                        if not word_text:
                            continue
                        words_list.append({
                            "word": word_text,
                            "start": float(word.get("start", start) or start),
                            "end": float(word.get("end", end) or end),
                        })
                else:
                    words_list.extend(_approximate_words_from_text(text, start, end))
        else:
            transcript_text = str(payload.get("text") or "").strip()
            duration = float(payload.get("duration") or 0.0) or 1.0
            if transcript_text:
                segments_list = [{"start": 0.0, "end": duration, "text": transcript_text}]
                words_list = _approximate_words_from_text(transcript_text, 0.0, duration)

    segments_file, words_file = _write_segments_and_words(output_dir, segments_list, words_list)

    # Prefer payload language metadata; fall back to local detection.
    payload_lang, payload_conf = _groq_language_from_payload(payload)
    lang_code = payload_lang or (detected[0] if detected else None)
    lang_conf = payload_conf if payload_conf is not None else (detected[1] if detected else None)
    _write_transcript_metadata(output_dir, lang_code, lang_conf)

    return segments_file, words_file


def _transcribe_with_openai_whisper(
    audio_path: str,
    output_dir: str,
    model_path: str,
    language: Optional[str] = None,
    translate: bool = False,
) -> Tuple[str, str]:
    import whisper
    import torch

    logger.info(f"Loading openai-whisper model from {model_path}")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model(model_path, device=device)

    task = "translate" if translate else "transcribe"
    logger.info(f"Starting transcription with openai-whisper (task={task}, language={language or 'auto'})...")
    result = model.transcribe(audio_path, word_timestamps=True, language=language, task=task)

    segments_list = []
    words_list = []

    for segment in result.get("segments", []):
        if not isinstance(segment, dict):
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", 0.0))
        text = str(segment.get("text", "")).strip()

        segments_list.append({
            "start": start,
            "end": end,
            "text": text
        })

        if "words" in segment and isinstance(segment["words"], list):
            for word in segment["words"]:
                if not isinstance(word, dict):
                    continue
                words_list.append({
                    "word": str(word.get("word", "")).strip(),
                    "start": float(word.get("start", start)),
                    "end": float(word.get("end", end))
                })
        else:
            words_list.extend(_approximate_words_from_text(text, start, end))

    segments_file, words_file = _write_segments_and_words(output_dir, segments_list, words_list)
    _write_transcript_metadata(output_dir, language, None)
    return segments_file, words_file


def transcribe_audio_file(
    audio_path: str,
    output_dir: str,
    whisper_model: Optional[str] = None,
    language: Optional[str] = None,
    translate: bool = False,
    task: Optional[str] = None,
    keep_native: bool = False,
    progress_callback: Optional[Callable[[str, int], None]] = None,
    **kwargs
) -> Tuple[str, str]:
    """Transcribe an audio file with optional language detection and translation.

    Args:
        audio_path: Path to the audio/video file.
        output_dir: Directory where segments.json / words.json (and
            transcript_info.json) are written.
        whisper_model: Backend selector: "groq", "tiny"/"base"/"small"/...,
            or "local_pt_<name>" for an OpenAI-whisper .pt checkpoint.
        language: Optional ISO-639-1 language override. When None, the
            language is auto-detected.
        translate: When True, output is always English (translated from the
            source language). Defaults to False for backward compatibility.
        task: Optional Whisper task ('transcribe' or 'translate'). Overrides translate flag if set.
        keep_native: If True and task=='translate', keep native language transcript files.

    Returns:
        Tuple of (segments_file, words_file) paths.
    """
    if task is not None:
        translate = (task == "translate")
    try:
        # If user explicitly selected groq
        if whisper_model == "groq":
            if settings.GROQ_API_KEY:
                logger.info("Using Groq Whisper Turbo for transcription")
                return _transcribe_with_groq(audio_path, output_dir, language, translate)
            else:
                logger.warning("Groq selected but no API key found. Falling back to local tiny.")
                whisper_model = "tiny"

        # If user explicitly selected a local PyTorch model (.pt)
        if whisper_model and whisper_model.startswith("local_pt_"):
            model_name = whisper_model.replace("local_pt_", "")
            pt_path = os.path.join(settings.WHISPER_PT_MODELS_DIR, f"{model_name}.pt")
            if os.path.exists(pt_path):
                logger.info(f"Using local PyTorch model: {pt_path}")
                return _transcribe_with_openai_whisper(audio_path, output_dir, pt_path, language, translate)
            else:
                logger.warning(f"Local PyTorch model not found: {pt_path}. Falling back to faster-whisper.")
                whisper_model = model_name  # Fallback to faster-whisper equivalent

        # If no specific model selected, use default logic
        if not whisper_model:
            provider = settings.TRANSCRIPTION_PROVIDER.lower()
            if provider == "groq" and settings.GROQ_API_KEY:
                logger.info("Using Groq Whisper Turbo for transcription")
                return _transcribe_with_groq(audio_path, output_dir, language, translate)
            whisper_model = settings.WHISPER_MODEL_SIZE

        logger.info(f"Using local faster-whisper transcription ({whisper_model})")
        return _transcribe_with_local_whisper(
            audio_path, output_dir, whisper_model, language, translate, progress_callback=progress_callback
        )

    except Exception as e:
        # A fallback run may only overwrite an empty/partial result. Without
        # this guard a failure in a later stage (metadata write, progress hook,
        # OOM at the very end) re-ran the tiny model over a good transcript and
        # shipped garbled captions that looked perfectly valid downstream.
        if _transcript_already_written(output_dir):
            logger.error(
                f"Transcription backend failed after a transcript was already written "
                f"to {output_dir}: {e}. Not falling back — the existing transcript is kept."
            )
            raise
        logger.warning(f"Primary transcription backend failed: {e}. Falling back to local faster-whisper (tiny).")
        try:
            return _transcribe_with_local_whisper(
                audio_path, output_dir, "tiny", language, translate, progress_callback=progress_callback
            )
        except Exception as local_error:
            logger.error(f"Transcription failed: {local_error}")
            raise Exception(f"Transcription failed: {local_error}")