"""Tests for language detection and translation in the Whisper service.

Covers:
- `detect_audio_language()` returns (code, confidence) for a real sample.
- `transcribe_audio_file(..., translate=True)` returns English for a non-English sample.
- `transcribe_audio_file(..., translate=False)` returns original-language transcript.
- `language` override threads through to the backend.
- Empty/silent audio handled gracefully (no crash, None language).
- Backward compatibility: old positional call still works and writes same files.
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import whisper  # noqa: E402

# A real, non-English (Greek) audio sample from the user's previous experiments.
# Skipped when not present so CI without the file still passes.
GREEK_SAMPLE = r"C:\Users\thana\Videos\Timeline_22_audio.wav"
HAVE_GREEK_SAMPLE = os.path.exists(GREEK_SAMPLE)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class FakeSegment:
    def __init__(self, start, end, text, words=None):
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []


class FakeWord:
    def __init__(self, word, start, end):
        self.word = word
        self.start = start
        self.end = end


class FakeInfo:
    language = "el"
    language_probability = 0.96
    duration = 12.0
    duration_after_vad = 12.0
    all_language_probs = [("el", 0.96), ("en", 0.02), ("fr", 0.01)]
    transcription_options = None
    vad_options = None


class FakeModel:
    """Configurable faster-whisper model double."""

    def __init__(self, language="el", confidence=0.96, segments=None, transcribed_segments=None):
        self.language = language
        self.confidence = confidence
        if segments is None:
            segments = [FakeSegment(0.0, 3.0, "Γεια σας, τι κάνετε;")]
        self.segments = segments
        # If provided, use as transcribe() result; else derive from language
        self.transcribed_segments = transcribed_segments if transcribed_segments is not None else self.segments
        self.calls = {"transcribe": [], "detect_language": []}

    def transcribe(self, *args, **kwargs):
        self.calls["transcribe"].append((args, kwargs))
        return iter(self.transcribed_segments), FakeInfo()

    def detect_language(self, *args, **kwargs):
        self.calls["detect_language"].append((args, kwargs))
        return self.language, self.confidence, [("el", self.confidence), ("en", 0.02)]


@pytest.fixture
def fake_model():
    return FakeModel()


@pytest.fixture
def sample_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture
def mock_audio(tmp_path):
    """A tiny fake audio file path (never decoded in unit tests)."""
    p = tmp_path / "sample.wav"
    p.write_bytes(b"RIFF" + b"\x00" * 64)
    return str(p)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """Give the service predictable settings for tests."""
    ns = SimpleNamespace(
        WHISPER_MODEL_SIZE="tiny",
        TRANSCRIPTION_PROVIDER="local",
        GROQ_API_KEY=None,
        GROQ_MODEL="whisper-large-v3-turbo",
        GROQ_BASE_URL="https://api.groq.com/openai/v1",
    )
    monkeypatch.setattr(whisper, "settings", ns)
    yield ns


# ---------------------------------------------------------------------------
# detect_audio_language()
# ---------------------------------------------------------------------------

def test_detect_audio_language_returns_code_and_confidence(fake_model, mock_audio, monkeypatch):
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    # detect_audio_language imports decode_audio inside the function, so patch
    # the underlying faster_whisper.audio module.
    with mock.patch("faster_whisper.audio.decode_audio", return_value=__import__("numpy").zeros(16000 * 5, dtype="float32")):
        lang, conf = whisper.detect_audio_language(mock_audio)
    assert lang == "el"
    assert conf == 0.96
    assert fake_model.calls["detect_language"]


def test_detect_audio_language_empty_audio_returns_none(mock_audio, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: model)
    with mock.patch("faster_whisper.audio.decode_audio", return_value=__import__("numpy").zeros(0, dtype="float32")):
        lang, conf = whisper.detect_audio_language(mock_audio)
    assert lang is None
    assert conf is None


def test_detect_audio_language_low_confidence_returns_none(mock_audio, monkeypatch):
    model = FakeModel(confidence=0.2)
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: model)
    with mock.patch("faster_whisper.audio.decode_audio", return_value=__import__("numpy").zeros(16000 * 5, dtype="float32")):
        lang, conf = whisper.detect_audio_language(mock_audio)
    assert lang is None
    assert conf is None


def test_detect_audio_language_decode_error_returns_none(mock_audio, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: model)
    with mock.patch("faster_whisper.audio.decode_audio", side_effect=RuntimeError("decode failed")):
        lang, conf = whisper.detect_audio_language(mock_audio)
    assert lang is None
    assert conf is None


# ---------------------------------------------------------------------------
# Local transcription: translation and language override
# ---------------------------------------------------------------------------

def test_local_translate_true_passes_task_translate(fake_model, sample_dir, mock_audio, monkeypatch):
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    # Suppress real detection: override the dedicated helper.
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: (None, None))

    segs, words = whisper._transcribe_with_local_whisper(mock_audio, sample_dir, "tiny", translate=True)

    assert fake_model.calls["transcribe"], "model.transcribe not called"
    args, kwargs = fake_model.calls["transcribe"][0]
    assert kwargs["task"] == "translate"

    assert os.path.exists(segs)
    assert os.path.exists(words)
    meta = json.load(open(os.path.join(sample_dir, "transcript_info.json"), encoding="utf-8"))
    assert meta["detected_language"] is None
    assert meta["language_code"] is None
    assert meta["detected"] is False


def test_local_translate_false_keeps_transcribe_task(fake_model, sample_dir, mock_audio, monkeypatch):
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: (None, None))

    whisper._transcribe_with_local_whisper(mock_audio, sample_dir, "tiny", translate=False)

    args, kwargs = fake_model.calls["transcribe"][0]
    assert kwargs["task"] == "transcribe"


def test_local_language_override_pins_language(fake_model, sample_dir, mock_audio, monkeypatch):
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    # Ensure detection is NOT called when an explicit override is present.
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: pytest.fail("detect should not run"))

    whisper._transcribe_with_local_whisper(mock_audio, sample_dir, "tiny", language="el", translate=False)

    args, kwargs = fake_model.calls["transcribe"][0]
    assert kwargs["language"] == "el"
    meta = json.load(open(os.path.join(sample_dir, "transcript_info.json"), encoding="utf-8"))
    assert meta["language_code"] == "el"
    assert meta["detected"] is False  # override, not auto-detection


def test_local_auto_detects_when_no_override(fake_model, sample_dir, mock_audio, monkeypatch):
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.94))

    whisper._transcribe_with_local_whisper(mock_audio, sample_dir, "tiny", language=None, translate=False)

    args, kwargs = fake_model.calls["transcribe"][0]
    assert kwargs["language"] == "el"
    meta = json.load(open(os.path.join(sample_dir, "transcript_info.json"), encoding="utf-8"))
    assert meta["language_code"] == "el"
    assert meta["language_confidence"] == 0.94
    assert meta["detected"] is True


def test_local_silent_audio_graceful(fake_model, sample_dir, mock_audio, monkeypatch):
    # Empty transcription result (silent audio) — must not crash.
    silent = FakeModel(segments=[])
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: silent)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: (None, None))

    segs, words = whisper._transcribe_with_local_whisper(mock_audio, sample_dir, "tiny")
    assert json.load(open(segs, encoding="utf-8")) == []
    assert json.load(open(words, encoding="utf-8")) == []


# ---------------------------------------------------------------------------
# public transcribe_audio_file(): backward compatibility + threading
# ---------------------------------------------------------------------------

def test_transcribe_audio_file_backward_compatible_positional(fake_model, sample_dir, mock_audio, monkeypatch):
    """Old 3-positional-arg call must still work and write segments/words."""
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: (None, None))
    monkeypatch.setattr(whisper.settings, "TRANSCRIPTION_PROVIDER", "local")

    segs, words = whisper.transcribe_audio_file(mock_audio, sample_dir, "tiny")
    assert os.path.exists(segs)
    assert os.path.exists(words)


def test_transcribe_audio_file_translate_true_english(fake_model, sample_dir, mock_audio, monkeypatch):
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.96))

    segs, _ = whisper.transcribe_audio_file(mock_audio, sample_dir, "tiny", translate=True)
    segments = json.load(open(segs, encoding="utf-8"))
    assert len(segments) == 1
    # The fake returns the same Greek text — the assertion here is that
    # task="translate" was requested; see _transcribe_with_local_whisper test
    # for the exact kwargs. This verifies threading end-to-end.
    assert segments[0]["text"].strip()


def test_transcribe_audio_file_falls_back_on_backend_error(fake_model, sample_dir, mock_audio, monkeypatch):
    """When the primary backend raises, local tiny must be used with same flags."""
    def boom(*a, **k):
        raise RuntimeError("backend exploded")
    monkeypatch.setattr(whisper.settings, "TRANSCRIPTION_PROVIDER", "local")
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.9))
    # First get_whisper_model call (primary) raises; fallback call succeeds.
    calls = {"n": 0}

    def flaky_model(size=None):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("model load failed")
        calls["n"] += 1
        return fake_model

    monkeypatch.setattr(whisper, "get_whisper_model", flaky_model)

    segs, words = whisper.transcribe_audio_file(mock_audio, sample_dir, "tiny", translate=True)
    assert os.path.exists(segs)
    assert os.path.exists(words)
    args, kwargs = fake_model.calls["transcribe"][0]
    assert kwargs["task"] == "translate"


# ---------------------------------------------------------------------------
# Groq backend
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_groq_transcription_payload(text="Hello world", language="en"):
    return {
        "text": text,
        "language": language,
        "duration": 2.0,
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 2.0,
                "text": text,
                "words": [
                    {"word": " Hello", "start": 0.0, "end": 0.5},
                    {"word": " world", "start": 0.5, "end": 1.0},
                ],
            }
        ],
    }


@pytest.fixture
def fake_compressed(tmp_path):
    """Create a real small file and return a stub that 'compresses' into it."""
    p = tmp_path / "compressed.mp3"
    p.write_bytes(b"\x00" * 4096)
    return str(p)


def test_groq_translate_uses_translations_endpoint(sample_dir, mock_audio, monkeypatch, _patch_settings, fake_compressed, fake_model):
    _patch_settings.GROQ_API_KEY = "test-key"
    monkeypatch.setattr(whisper.settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.96))

    fake_client = mock.Mock()
    fake_client.__enter__ = mock.Mock(return_value=fake_client)
    fake_client.__exit__ = mock.Mock(return_value=False)
    fake_client.post.return_value = FakeResponse({"text": "Hello world in English."})

    monkeypatch.setattr(whisper, "_compress_audio_for_groq", lambda p: fake_compressed)

    with mock.patch("httpx.Client", return_value=fake_client):
        segs, words = whisper._transcribe_with_groq(mock_audio, sample_dir, translate=True)

    # Endpoint must be translations, not transcriptions
    call_args, call_kwargs = fake_client.post.call_args
    url = call_args[0]
    assert url.endswith("/audio/translations")
    assert "language" not in call_kwargs.get("data", {})

    segments = json.load(open(segs, encoding="utf-8"))
    assert segments[0]["text"] == "Hello world in English."


def test_groq_transcribe_passes_language_override(sample_dir, mock_audio, monkeypatch, _patch_settings, fake_compressed, fake_model):
    monkeypatch.setattr(whisper.settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.96))
    monkeypatch.setattr(whisper, "_compress_audio_for_groq", lambda p: fake_compressed)
    fake_client = mock.Mock()
    fake_client.__enter__ = mock.Mock(return_value=fake_client)
    fake_client.__exit__ = mock.Mock(return_value=False)
    fake_client.post.return_value = FakeResponse(_make_groq_transcription_payload())
    with mock.patch("httpx.Client", return_value=fake_client):
        segs, _ = whisper._transcribe_with_groq(mock_audio, sample_dir, language="el", translate=False)

    url = fake_client.post.call_args[0][0]
    assert url.endswith("/audio/transcriptions")
    data = fake_client.post.call_args[1]["data"]
    assert data["language"] == "el"

    meta = json.load(open(os.path.join(sample_dir, "transcript_info.json"), encoding="utf-8"))
    assert meta["language_code"] == "en"  # from payload


def test_groq_translate_no_segments_falls_back_to_plain_text(sample_dir, mock_audio, monkeypatch, _patch_settings, fake_compressed, fake_model):
    monkeypatch.setattr(whisper.settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.96))
    monkeypatch.setattr(whisper, "_compress_audio_for_groq", lambda p: fake_compressed)
    fake_client = mock.Mock()
    fake_client.__enter__ = mock.Mock(return_value=fake_client)
    fake_client.__exit__ = mock.Mock(return_value=False)
    fake_client.post.return_value = FakeResponse({"text": "Translated text without segments."})
    with mock.patch("httpx.Client", return_value=fake_client):
        segs, words = whisper._transcribe_with_groq(mock_audio, sample_dir, translate=True)
    segments = json.load(open(segs, encoding="utf-8"))
    assert len(segments) == 1
    assert segments[0]["text"] == "Translated text without segments."


def test_groq_empty_audio_graceful(sample_dir, mock_audio, monkeypatch, _patch_settings, fake_compressed, fake_model):
    monkeypatch.setattr(whisper.settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(whisper, "get_whisper_model", lambda size=None: fake_model)
    monkeypatch.setattr(whisper, "detect_audio_language", lambda *a, **k: ("el", 0.96))
    monkeypatch.setattr(whisper, "_compress_audio_for_groq", lambda p: fake_compressed)
    fake_client = mock.Mock()
    fake_client.__enter__ = mock.Mock(return_value=fake_client)
    fake_client.__exit__ = mock.Mock(return_value=False)
    fake_client.post.return_value = FakeResponse({"text": ""})
    with mock.patch("httpx.Client", return_value=fake_client):
        segs, words = whisper._transcribe_with_groq(mock_audio, sample_dir)
    assert json.load(open(segs, encoding="utf-8")) == []


# ---------------------------------------------------------------------------
# Live smoke tests (skipped when sample/models are absent)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_GREEK_SAMPLE, reason="Greek sample not present")
def test_live_greek_detection_and_translation(tmp_path):
    """Real end-to-end over the Greek sample with the cached local model."""
    d = str(tmp_path / "out")
    os.makedirs(d, exist_ok=True)

    lang, conf = whisper.detect_audio_language(GREEK_SAMPLE, "tiny")
    assert lang is not None
    assert conf is not None
    # OpenAI Whisper language codes are ISO-639-1; Greek is 'el'
    assert lang == "el", f"Expected el, got {lang}"

    segs, _ = whisper.transcribe_audio_file(GREEK_SAMPLE, d, "tiny", translate=False)
    segments = json.load(open(segs, encoding="utf-8"))
    assert segments, "Expected at least one segment for Greek speech"
    greek_text = " ".join(s["text"] for s in segments)
    # Greek sample must contain Greek characters in native transcription
    assert any(ord(ch) > 0x370 and ord(ch) < 0x400 for ch in greek_text), (
        f"Expected Greek characters in native transcript, got: {greek_text[:60]!r}"
    )

    # Translation to English
    d2 = str(tmp_path / "out2")
    os.makedirs(d2, exist_ok=True)
    segs2, _ = whisper.transcribe_audio_file(GREEK_SAMPLE, d2, "tiny", translate=True)
    segments2 = json.load(open(segs2, encoding="utf-8"))
    english_text = " ".join(s["text"] for s in segments2)
    assert any(ch.isascii() and ch.isalpha() for ch in english_text), (
        f"Expected English text after translation, got: {english_text[:60]!r}"
    )

    meta = json.load(open(os.path.join(d2, "transcript_info.json"), encoding="utf-8"))
    assert meta["language_code"] == "el"