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


# ---------------------------------------------------------------------------
# stage_diarize: who speaks when, soft-failing into the old behaviour
# ---------------------------------------------------------------------------

def _run_diarize(job_dir):
    return asyncio.run(worker.stage_diarize(
        1, job_dir, str(job_dir / "source.mp4"), lambda stage, percent: None))


class TestStageDiarize:
    def test_turns_are_written_and_a_resume_never_re_runs_the_model(self, job_dir, monkeypatch):
        import services.diarization as diar
        calls = []

        def fake_try_diarize(path):
            calls.append(path)
            return [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.2}]

        monkeypatch.setattr(diar, "try_diarize", fake_try_diarize)

        turns = _run_diarize(job_dir)

        assert turns == [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.2}]
        assert len(calls) == 1
        data = json.loads((job_dir / "diarization.json").read_text(encoding="utf-8"))
        assert data["turns"] == turns

        def explode(path):
            raise AssertionError("a resumed job must not re-run diarization")

        monkeypatch.setattr(diar, "try_diarize", explode)
        assert _run_diarize(job_dir) == turns

    def test_disabled_diarization_is_skipped_entirely(self, job_dir, monkeypatch):
        import services.diarization as diar

        def explode(path):
            raise AssertionError("DIARIZATION_ENABLED=false must not run pyannote")

        monkeypatch.setattr(diar, "try_diarize", explode)
        monkeypatch.setattr(worker.settings, "DIARIZATION_ENABLED", False)

        assert _run_diarize(job_dir) == []
        assert not (job_dir / "diarization.json").exists()

    def test_an_unavailable_diarizer_is_a_warning_not_a_failure(self, job_dir, monkeypatch):
        import services.diarization as diar
        monkeypatch.setattr(diar, "try_diarize", lambda path: None)

        assert _run_diarize(job_dir) == []
        assert not (job_dir / "diarization.json").exists()

    def test_a_broken_diarizer_never_kills_the_job(self, job_dir, monkeypatch):
        import services.diarization as diar

        def explode(path):
            raise RuntimeError("torch exploded")

        monkeypatch.setattr(diar, "try_diarize", explode)
        assert _run_diarize(job_dir) == []


# ---------------------------------------------------------------------------
# per-clip diarization slice
# ---------------------------------------------------------------------------

class TestClipDiarizationSlice:
    def test_turns_are_sliced_and_re_zeroed_to_clip_time(self, job_dir):
        (job_dir / "diarization.json").write_text(json.dumps({"turns": [
            {"speaker": "A", "start": 0.5, "end": 2.0},
            {"speaker": "B", "start": 11.0, "end": 13.5},
            {"speaker": "A", "start": 20.0, "end": 22.0},
        ]}), encoding="utf-8")

        assert worker.load_clip_diarization(job_dir, 10.0, 15.0) == [
            {"speaker": "B", "start": 1.0, "end": 3.5},
        ]

    def test_missing_or_broken_diarization_degrades_to_empty(self, job_dir):
        assert worker.load_clip_diarization(job_dir, 0.0, 10.0) == []
        (job_dir / "diarization.json").write_text("{not json", encoding="utf-8")
        assert worker.load_clip_diarization(job_dir, 0.0, 10.0) == []


# ---------------------------------------------------------------------------
# scene attention: what the clip is about, soft-failing into no new signal
# ---------------------------------------------------------------------------

def _run_attention(job_dir, monkeypatch, builder, enabled=True):
    import services.reframe_plan as reframe_plan
    import services.scene_attention as scene_attention
    from config import settings

    monkeypatch.setattr(settings, "SCENE_ATTENTION_ENABLED", enabled)
    monkeypatch.setattr(reframe_plan, "analyse_clip",
                        lambda path, sample_interval=0.5: {"samples": [], "tracks": []})
    monkeypatch.setattr(scene_attention, "build_attention", builder)
    return asyncio.run(worker.stage_scene_attention(
        1, job_dir, str(job_dir / "short.mp4"), str(job_dir / "words.json"), "hello world"))


class TestStageSceneAttention:
    def test_attention_is_written_to_the_clip_dir_for_debugging(self, job_dir, monkeypatch):
        scenes = [{"start": 1.0, "end": 3.0, "target": "laptop", "importance": 0.8,
                   "visibility": 1.0, "source": "keyword", "boxes": [[1.0, [900, 300, 500, 350]]]}]

        async def builder(analysis, words_file, transcript):
            return {"scenes": scenes, "notes": ["keyword tier: 1 scene"]}

        attention = _run_attention(job_dir, monkeypatch, builder)

        assert attention["scenes"] == scenes
        written = json.loads((job_dir / "attention.json").read_text(encoding="utf-8"))
        assert written["scenes"][0]["target"] == "laptop"

    def test_disabled_attention_never_touches_a_model_or_writes_a_file(self, job_dir, monkeypatch):
        async def builder(analysis, words_file, transcript):
            raise AssertionError("SCENE_ATTENTION_ENABLED=false must not build attention")

        assert _run_attention(job_dir, monkeypatch, builder, enabled=False) is None
        assert not (job_dir / "attention.json").exists()

    def test_a_broken_attention_builder_is_only_a_log_line(self, job_dir, monkeypatch):
        async def builder(analysis, words_file, transcript):
            raise RuntimeError("gateway exploded")

        assert _run_attention(job_dir, monkeypatch, builder) is None
        assert not (job_dir / "attention.json").exists()
