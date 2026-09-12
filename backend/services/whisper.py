"""
Service for audio transcription using Groq Whisper Turbo by default,
with local faster-whisper as a resilient fallback.
"""
import json
import os
import subprocess
from typing import List, Tuple

import httpx
from loguru import logger

from faster_whisper import WhisperModel
from config import settings

# Global model instance (loaded once, reused across jobs)
_model: WhisperModel = None
_model_size: str = None


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


def get_whisper_model() -> WhisperModel:
    global _model, _model_size

    model_size = settings.WHISPER_MODEL_SIZE

    if _model is None or _model_size != model_size:
        logger.info(f"Loading faster-whisper model: {model_size}")
        try:
            _model = WhisperModel(model_size, device="cpu", compute_type="int8")
        except Exception:
            logger.warning(f"Failed to load '{model_size}', falling back to 'tiny'")
            _model = WhisperModel("tiny", device="cpu", compute_type="int8")
        _model_size = model_size
        logger.info("Whisper model loaded")

    return _model


def _transcribe_with_local_whisper(audio_path: str, output_dir: str) -> Tuple[str, str]:
    model = get_whisper_model()
    segments, _ = model.transcribe(
        audio_path,
        word_timestamps=True,
        vad_filter=True,
        language=None,
        beam_size=1,
    )

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

    segments_file = os.path.join(output_dir, "segments.json")
    with open(segments_file, "w", encoding="utf-8") as f:
        json.dump(segments_list, f, ensure_ascii=False, indent=2)

    words_file = os.path.join(output_dir, "words.json")
    with open(words_file, "w", encoding="utf-8") as f:
        json.dump(words_list, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Local transcription saved to {segments_file} and {words_file} "
        f"({len(segments_list)} segments, {len(words_list)} words)"
    )
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


def _transcribe_with_groq(audio_path: str, output_dir: str) -> Tuple[str, str]:
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

    with open(groq_audio, "rb") as f:
        files = {"file": (os.path.basename(groq_audio), f, "application/octet-stream")}
        data = {
            "model": settings.GROQ_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word", "segment"],
        }
        headers = {"Authorization": f"Bearer {api_key}"}

        with httpx.Client(timeout=300.0) as client:
            response = client.post(
                f"{settings.GROQ_BASE_URL.rstrip('/')}/audio/transcriptions",
                files=files,
                data=data,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()

    segments_payload = payload.get("segments") or []
    segments_list: List[dict] = []
    words_list: List[dict] = []

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

    segments_file = os.path.join(output_dir, "segments.json")
    with open(segments_file, "w", encoding="utf-8") as f:
        json.dump(segments_list, f, ensure_ascii=False, indent=2)

    words_file = os.path.join(output_dir, "words.json")
    with open(words_file, "w", encoding="utf-8") as f:
        json.dump(words_list, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Groq transcription saved to {segments_file} and {words_file} "
        f"({len(segments_list)} segments, {len(words_list)} words)"
    )
    return segments_file, words_file


def transcribe_audio_file(audio_path: str, output_dir: str) -> Tuple[str, str]:
    try:
        provider = settings.TRANSCRIPTION_PROVIDER.lower()
        if provider == "groq" and settings.GROQ_API_KEY:
            logger.info("Using Groq Whisper Turbo for transcription")
            return _transcribe_with_groq(audio_path, output_dir)

        logger.info("Using local faster-whisper transcription fallback")
        return _transcribe_with_local_whisper(audio_path, output_dir)

    except Exception as e:
        logger.warning(f"Primary transcription backend failed: {e}. Falling back to local faster-whisper.")
        try:
            return _transcribe_with_local_whisper(audio_path, output_dir)
        except Exception as local_error:
            logger.error(f"Transcription failed: {local_error}")
            raise Exception(f"Transcription failed: {local_error}")