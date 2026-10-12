"""Regression tests for the worker pipeline stages lifted out of process_job_async.

stage_transcribe is the one that carried the multilingual bug: the UI's
Auto-Translate checkbox set job.llm_translate, which pointed at a post-transcript
LLM pass whose helpers were never written in services.whisper. The ImportError was
caught and logged as "LLM Translation failed", so every job silently kept the
original language. Auto-Translate now selects Whisper's own translate task.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import worker


@pytest.fixture
def job_dir(tmp_path, monkeypatch):
    """A job directory; transcript files are written per test."""
    monkeypatch.setattr(worker.settings, "DELETE_AUDIO_AFTER_TRANSCRIPTION", False, raising=False)
    return tmp_path


def _job(**overrides):
    job = SimpleNamespace(
        whisper_model="tiny",
        transcription_task=None,
        source_language=None,
        keep_native_transcript=False,
        llm_translate=False,
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def _write_transcript(dir_path, segments, words):
    (dir_path / "segments.json").write_text(json.dumps(segments), encoding="utf-8")
    (dir_path / "words.json").write_text(json.dumps(words), encoding="utf-8")


def _run(job_dir, job):
    return asyncio.run(worker.stage_transcribe(
        1, job, job_dir, str(job_dir / "audio.wav"), lambda stage, percent: None))


def test_auto_translate_flag_selects_whisper_translate_task(job_dir, monkeypatch):
    seen = {}

    def fake_transcribe(audio_path, output_dir, model, **kwargs):
        seen.update(kwargs)
        _write_transcript(job_dir, [{"start": 0.0, "end": 1.5, "text": "hello world"}],
                          [{"word": "hello", "start": 0.0, "end": 0.7}])
        return str(job_dir / "segments.json"), str(job_dir / "words.json")

    monkeypatch.setattr(worker, "transcribe_audio_file", fake_transcribe)

    segments = _run(job_dir, _job(llm_translate=True))[0]

    assert seen["task"] == "translate", "Auto-Translate must reach the transcriber"
    assert segments[0]["text"] == "hello world"


def test_job_without_the_flag_transcribes(job_dir, monkeypatch):
    seen = {}

    def fake_transcribe(audio_path, output_dir, model, **kwargs):
        seen.update(kwargs)
        _write_transcript(job_dir, [{"start": 0.0, "end": 1.0, "text": "hola"}], [])
        return str(job_dir / "segments.json"), str(job_dir / "words.json")

    monkeypatch.setattr(worker, "transcribe_audio_file", fake_transcribe)

    _run(job_dir, _job())

    assert seen["task"] == "transcribe"


def test_explicit_transcription_task_still_wins(job_dir, monkeypatch):
    seen = {}

    def fake_transcribe(audio_path, output_dir, model, **kwargs):
        seen.update(kwargs)
        _write_transcript(job_dir, [], [])
        return str(job_dir / "segments.json"), str(job_dir / "words.json")

    monkeypatch.setattr(worker, "transcribe_audio_file", fake_transcribe)

    _run(job_dir, _job(llm_translate=True, transcription_task="transcribe",
                       source_language="el", keep_native_transcript=True))

    assert seen["task"] == "transcribe"
    assert seen["language"] == "el"
    assert seen["keep_native"] is True


def test_resume_reuses_the_transcript_on_disk(job_dir, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a resumed job must not re-run Whisper")

    _write_transcript(job_dir, [{"start": 0.0, "end": 1.0, "text": "hi"}],
                      [{"word": "hi", "start": 0.0, "end": 0.5}])
    monkeypatch.setattr(worker, "transcribe_audio_file", explode)

    segments = _run(job_dir, _job())[0]

    assert segments == [{"start": 0.0, "end": 1.0, "text": "hi"}]


def test_source_metadata_is_returned(job_dir, monkeypatch):
    _write_transcript(job_dir, [], [])
    (job_dir / "meta.json").write_text(
        json.dumps({"title": "My video", "uploader": "Some channel"}), encoding="utf-8")

    _segments, _segments_file, _words_file, title, channel = _run(job_dir, _job())

    assert (title, channel) == ("My video", "Some channel")
