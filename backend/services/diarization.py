"""Speaker diarization via pyannote.audio, with a soft-fail contract.

Every entry point here can only ever return turns or ``None`` — a job must
never die because a model download was gated or a token expired. The pipeline
is fed an in-memory waveform dict instead of a file path: pyannote 4.x routes
file decoding through torchcodec, which is fragile on Windows, and its own
warning recommends the waveform form (pyannote/audio/core/io.py).
"""
import os
import threading
from typing import Any, Dict, List, Optional

from loguru import logger

from config import settings

_PIPELINE: Optional[Any] = None
_PIPELINE_LOCK = threading.Lock()


def _hf_token() -> str:
    return settings.HF_KEY or os.getenv("HUGGINGFACE_TOKEN", "") or ""


def _construct_pipeline(model_id: str, token: str):
    # The only place pyannote is imported: keep the heavy dependency lazy so a
    # machine without the models still boots.
    from pyannote.audio import Pipeline

    return Pipeline.from_pretrained(model_id, token=token)


def _load_pipeline():
    """The pipeline is a few hundred MB of weights — build it once per process."""
    global _PIPELINE
    with _PIPELINE_LOCK:
        if _PIPELINE is None:
            _PIPELINE = _construct_pipeline(settings.DIARIZATION_MODEL, _hf_token())
        return _PIPELINE


def _decode_to_waveform(audio_path: str) -> Dict[str, Any]:
    """ffmpeg-decoded mono float32 @16 kHz, shaped (channel, time) for pyannote.

    Reuses the decoder transcription already depends on, so PyAV/torchcodec
    never touch the file.
    """
    import torch

    from services.audio import TRANSCRIPTION_SAMPLE_RATE, decode_audio_for_transcription

    samples = decode_audio_for_transcription(audio_path)
    if samples.size == 0:
        raise RuntimeError(f"no audio samples decoded from {audio_path}")
    return {
        "waveform": torch.from_numpy(samples).unsqueeze(0),
        "sample_rate": TRANSCRIPTION_SAMPLE_RATE,
        "uri": os.path.splitext(os.path.basename(audio_path))[0],
    }


def _turns_from_result(result: Any) -> List[Dict[str, Any]]:
    # 4.x wraps the annotation in DiarizeOutput; older/legacy pipelines return
    # the annotation itself.
    annotation = getattr(result, "speaker_diarization", result)
    return [
        {
            "speaker": speaker,
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
        }
        for segment, _track, speaker in annotation.itertracks(yield_label=True)
    ]


def try_diarize(audio_path: str) -> Optional[List[Dict[str, Any]]]:
    """Speaker turns, or None when diarization is unavailable. Never raises.

    ``[]`` is a finding (nobody spoke); ``None`` is a failure (no token, gated
    model, undecodable audio) — callers treat them differently.
    """
    try:
        if not _hf_token():
            logger.info(
                "Diarization skipped: no HuggingFace token configured (HF_KEY); "
                "single-speaker heuristics remain in effect"
            )
            return None
        payload = _decode_to_waveform(audio_path)
        result = _load_pipeline()(payload)
        return _turns_from_result(result)
    except Exception as exc:
        logger.warning(
            f"Diarization failed ({type(exc).__name__}: {exc}); continuing without speaker turns"
        )
        return None


def slice_turns(
    turns: Optional[List[Dict[str, Any]]], start: float, end: float
) -> List[Dict[str, Any]]:
    """The turns overlapping one clip window, re-zeroed to clip-local time."""
    sliced: List[Dict[str, Any]] = []
    for turn in turns or []:
        overlap_start = max(float(turn["start"]), start)
        overlap_end = min(float(turn["end"]), end)
        if overlap_end <= overlap_start:
            continue
        sliced.append(
            {
                "speaker": turn["speaker"],
                "start": round(overlap_start - start, 3),
                "end": round(overlap_end - start, 3),
            }
        )
    return sliced


def assign_speakers_to_words(
    words: List[Dict[str, Any]], turns: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Copy of ``words`` with a ``speaker`` key on every word a turn covers.

    A word gets the speaker of the turn it overlaps most; words in gaps keep no
    speaker (silence is not a speaker). The input list is not mutated.
    """
    if not turns:
        return list(words)
    assigned: List[Dict[str, Any]] = []
    for word in words:
        best_speaker = None
        best_overlap = 0.0
        for turn in turns:
            overlap = min(word["end"], turn["end"]) - max(word["start"], turn["start"])
            if overlap > best_overlap:
                best_overlap, best_speaker = overlap, turn["speaker"]
        enriched = dict(word)
        if best_speaker is not None:
            enriched["speaker"] = best_speaker
        assigned.append(enriched)
    return assigned


def diarize_audio(audio_path: str) -> List[Dict[str, Any]]:
    """Legacy wrapper: a single default speaker when analysis is unavailable."""
    turns = try_diarize(audio_path)
    if turns is not None:
        return turns
    return [{"speaker": "SPEAKER_00", "start": 0.0, "end": 9999.0}]
