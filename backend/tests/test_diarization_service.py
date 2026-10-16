"""The diarization adapter — everything except the gated model itself.

pyannote weighs hundreds of MB and needs an accepted-license HuggingFace
token, so the pipeline is a stub that mirrors the API the *installed*
pyannote-audio 4.0.7 actually exposes. What is under test is exactly the
adapter the legacy module got wrong: ``use_auth_token`` (removed in 4.x) vs
``token=``, the ``DiarizeOutput`` wrapper 4.x returns instead of a bare
Annotation, the ``HF_KEY`` env name this machine uses (not
``HUGGINGFACE_TOKEN``), and handing the pipeline an in-memory waveform dict so
torchcodec never decodes a file (it breaks on Windows ffmpeg DLLs).
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from config import settings
from services import diarization

requires_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg on PATH")


class _Segment:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class _Annotation:
    """Minimal stand-in for pyannote.core.Annotation."""

    def __init__(self, spans):
        self._spans = spans  # [(start, end, speaker), ...]

    def itertracks(self, yield_label=False):
        for start, end, speaker in self._spans:
            yield _Segment(start, end), None, speaker


class _DiarizeOutput:
    """The 4.x dataclass: itertracks lives on .speaker_diarization, not here."""

    def __init__(self, annotation):
        self.speaker_diarization = annotation
        self.exclusive_speaker_diarization = annotation
        self.speaker_embeddings = None


class _StubPipeline:
    def __init__(self, result):
        self.result = result
        self.received = None

    def __call__(self, file, **kwargs):
        self.received = file
        return self.result


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """A token that is definitely fake, and no cached pipeline between tests."""
    monkeypatch.setattr(diarization, "_PIPELINE", None)
    monkeypatch.setattr(settings, "HF_KEY", "hf_test_key")
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)


def _wire(monkeypatch, result):
    pipeline = _StubPipeline(result)
    monkeypatch.setattr(diarization, "_load_pipeline", lambda: pipeline)
    monkeypatch.setattr(
        diarization, "_decode_to_waveform",
        lambda path: {"waveform": "TENSOR", "sample_rate": 16000, "uri": "job"},
    )
    return pipeline


def _raise(exc):
    def _fail(*_args, **_kwargs):
        raise exc
    return _fail


# --------------------------------------------------------------------------
# pyannote 4.x return shapes
# --------------------------------------------------------------------------

def test_diarize_output_from_pyannote4_is_unwrapped(monkeypatch):
    annotation = _Annotation([(0.0, 4.5, "SPEAKER_00"), (4.5, 9.0, "SPEAKER_01")])
    _wire(monkeypatch, _DiarizeOutput(annotation))
    turns = diarization.try_diarize("audio.wav")
    assert turns == [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 4.5},
        {"speaker": "SPEAKER_01", "start": 4.5, "end": 9.0},
    ]


def test_a_legacy_annotation_return_is_also_accepted(monkeypatch):
    annotation = _Annotation([(1.0, 2.0, "SPEAKER_00")])
    _wire(monkeypatch, annotation)
    turns = diarization.try_diarize("audio.wav")
    assert turns == [{"speaker": "SPEAKER_00", "start": 1.0, "end": 2.0}]


def test_an_empty_annotation_is_an_empty_list_not_a_fallback(monkeypatch):
    """Silence is a finding: 'nobody speaks', not 'diarization unavailable'."""
    _wire(monkeypatch, _DiarizeOutput(_Annotation([])))
    assert diarization.try_diarize("audio.wav") == []


# --------------------------------------------------------------------------
# the input the pipeline receives — torchcodec must never touch the file
# --------------------------------------------------------------------------

def test_try_diarize_passes_a_waveform_dict_not_a_path(monkeypatch):
    pipeline = _wire(monkeypatch, _DiarizeOutput(_Annotation([])))
    diarization.try_diarize("audio.wav")
    assert isinstance(pipeline.received, dict)
    assert pipeline.received["sample_rate"] == 16000
    assert "waveform" in pipeline.received


@requires_ffmpeg
def test_decode_to_waveform_is_channel_first_float32_at_16k(tmp_path):
    import torch

    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.5", "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )
    payload = diarization._decode_to_waveform(str(wav))
    assert payload["sample_rate"] == 16000
    assert payload["uri"] == "tone"
    waveform = payload["waveform"]
    assert isinstance(waveform, torch.Tensor)
    assert waveform.dtype == torch.float32
    assert waveform.dim() == 2 and waveform.shape[0] == 1
    assert abs(waveform.shape[1] - 8000) < 100


# --------------------------------------------------------------------------
# soft-failure contract — the pipeline must never break a job
# --------------------------------------------------------------------------

def test_missing_hf_token_returns_none_without_loading_the_model(monkeypatch):
    monkeypatch.setattr(settings, "HF_KEY", "")
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    loads = []
    monkeypatch.setattr(diarization, "_load_pipeline", lambda: loads.append(1))
    assert diarization.try_diarize("audio.wav") is None
    assert loads == []


def test_undecodable_audio_fails_fast_before_the_model_loads(monkeypatch, tmp_path):
    """dummy.wav and gated models: decode first so a bad path never costs a
    multi-hundred-MB model download."""
    loads = []
    monkeypatch.setattr(diarization, "_load_pipeline", lambda: loads.append(1))
    assert diarization.try_diarize(str(tmp_path / "missing.wav")) is None
    assert loads == []


def test_pipeline_load_failure_returns_none(monkeypatch):
    monkeypatch.setattr(diarization, "_decode_to_waveform",
                        lambda path: {"waveform": "T", "sample_rate": 16000})
    monkeypatch.setattr(diarization, "_load_pipeline", _raise(RuntimeError("gated")))
    assert diarization.try_diarize("audio.wav") is None


def test_pipeline_runtime_failure_returns_none(monkeypatch):
    class _Exploding:
        def __call__(self, file, **kwargs):
            raise RuntimeError("segmentation failed")

    monkeypatch.setattr(diarization, "_decode_to_waveform",
                        lambda path: {"waveform": "T", "sample_rate": 16000})
    monkeypatch.setattr(diarization, "_load_pipeline", lambda: _Exploding())
    assert diarization.try_diarize("audio.wav") is None


def test_the_model_is_constructed_once_and_reused(monkeypatch):
    calls = []
    pipeline = _StubPipeline(_DiarizeOutput(_Annotation([])))

    def fake_construct(model_id, token):
        calls.append((model_id, token))
        return pipeline

    monkeypatch.setattr(diarization, "_construct_pipeline", fake_construct)
    monkeypatch.setattr(diarization, "_decode_to_waveform",
                        lambda path: {"waveform": "T", "sample_rate": 16000})
    assert diarization.try_diarize("a.wav") == []
    assert diarization.try_diarize("b.wav") == []
    assert calls == [(settings.DIARIZATION_MODEL, "hf_test_key")]


# --------------------------------------------------------------------------
# the legacy wrapper keeps its contract (pinned live by test_upgrades too)
# --------------------------------------------------------------------------

def test_diarize_audio_falls_back_when_analysis_is_unavailable(monkeypatch):
    monkeypatch.setattr(diarization, "try_diarize", lambda path: None)
    assert diarization.diarize_audio("dummy.wav") == [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 9999.0},
    ]


def test_diarize_audio_passes_real_turns_through(monkeypatch):
    turns = [{"speaker": "SPEAKER_01", "start": 0.0, "end": 2.0}]
    monkeypatch.setattr(diarization, "try_diarize", lambda path: turns)
    assert diarization.diarize_audio("dummy.wav") == turns


# --------------------------------------------------------------------------
# clip-window slicing and word assignment
# --------------------------------------------------------------------------

def test_slice_turns_rezeros_to_the_clip_window():
    turns = [
        {"speaker": "A", "start": 10.0, "end": 20.0},
        {"speaker": "B", "start": 25.0, "end": 35.0},
    ]
    assert diarization.slice_turns(turns, 12.0, 30.0) == [
        {"speaker": "A", "start": 0.0, "end": 8.0},
        {"speaker": "B", "start": 13.0, "end": 18.0},
    ]


def test_slice_turns_drops_turns_outside_the_window():
    turns = [
        {"speaker": "A", "start": 0.0, "end": 5.0},
        {"speaker": "B", "start": 40.0, "end": 50.0},
    ]
    assert diarization.slice_turns(turns, 10.0, 30.0) == []
    assert diarization.slice_turns(None, 10.0, 30.0) == []


def test_assign_speakers_uses_max_overlap_and_skips_gaps():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.5},
        {"word": "silence", "start": 1.5, "end": 1.7},
    ]
    turns = [
        {"speaker": "A", "start": 0.0, "end": 0.3},
        {"speaker": "B", "start": 0.3, "end": 1.0},
    ]
    assigned = diarization.assign_speakers_to_words(words, turns)
    assert assigned[0]["speaker"] == "A"  # 0.3s of overlap beats B's 0.2s
    assert "speaker" not in assigned[1]
    assert "speaker" not in words[0]  # input list is not mutated
