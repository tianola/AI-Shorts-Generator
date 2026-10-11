"""Transcription decoding.

faster-whisper 1.2.1 calls av.open(metadata_errors=...), an argument PyAV 19
removed, so every local transcription raised TypeError before reading a single
sample. The pipeline decodes with ffmpeg instead and hands the model an array.
These tests pin that, because a refactor back to a path argument would silently
re-break the worker.
"""
import os
import subprocess
from types import SimpleNamespace

import pytest

from services import whisper as whisper_service


def _ffmpeg_available() -> bool:
    from shutil import which

    if not (which("ffmpeg") and which("ffprobe")):
        return False
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
    except OSError:
        return False


requires_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(), reason="requires ffmpeg on PATH")


@pytest.fixture
def wav16k(tmp_path):
    """Two seconds of a 440 Hz tone, written at 44.1 kHz like extract_audio does."""
    path = str(tmp_path / "audio.wav")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ar", "44100", path],
        check=True, capture_output=True,
    )
    return path


@requires_ffmpeg
def test_decoder_resamples_to_mono_float32_16k(wav16k):
    import numpy as np

    from services.audio import decode_audio_for_transcription

    pcm = decode_audio_for_transcription(wav16k)

    assert pcm.dtype == np.float32
    assert abs(len(pcm) / 16000 - 2.0) < 0.05
    assert 0.1 < np.abs(pcm).max() <= 1.0


@requires_ffmpeg
def test_decoder_honours_the_window(wav16k):
    from services.audio import decode_audio_for_transcription

    pcm = decode_audio_for_transcription(wav16k, duration=1.0)

    assert abs(len(pcm) / 16000 - 1.0) < 0.1


@requires_ffmpeg
def test_decoder_reports_a_file_it_cannot_read(tmp_path):
    from services.audio import decode_audio_for_transcription

    bogus = str(tmp_path / "not-audio.wav")
    with open(bogus, "wb") as f:
        f.truncate(1024)

    with pytest.raises(RuntimeError):
        decode_audio_for_transcription(bogus)


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter([]), SimpleNamespace(duration=120.0)


@requires_ffmpeg
def test_transcription_passes_samples_not_a_path(tmp_path, wav16k, monkeypatch):
    """A path argument sends faster-whisper back through the broken PyAV decoder."""
    import numpy as np

    model = FakeModel()
    monkeypatch.setattr(whisper_service, "get_whisper_model", lambda size=None: model)

    whisper_service._transcribe_with_local_whisper(wav16k, str(tmp_path),
                                                   model_size="tiny", language="en")
    audio, kwargs = model.calls[0]
    assert isinstance(audio, np.ndarray)
    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["beam_size"] == 1


def test_language_detection_only_reads_a_short_window(monkeypatch):
    seen = {}

    def fake_decode(path, start=0.0, duration=None):
        seen["duration"] = duration
        return []

    monkeypatch.setattr(whisper_service, "decode_audio_for_transcription", fake_decode)

    assert whisper_service.detect_audio_language("whatever.wav", "tiny") == (None, None)
    assert seen["duration"] == 30.0
