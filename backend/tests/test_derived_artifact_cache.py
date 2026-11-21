"""Derived per-clip/job artifacts must be keyed by the inputs that produced them.

The bug this file exists to kill (live on job 110, 2026-11-12): every derived
artifact was skipped on ``os.path.exists`` alone and stored under a directory
named by the clip *index*. ``worker.py``'s end-of-render cleanup deletes
``short.mp4``/``captions.ass`` but keeps ``words.json``, so a re-run whose
selector picked different windows re-cut the picture and then reused the
0-30 s transcript that the previous run had left in ``clip_0/``. The result was
a frame from t=3175 s carrying the caption "own. Something the legacy" from
t=0 s — three shorts, all of them wrong, all of them published-eligible.

Existence is not evidence. The rule these tests hold: an artifact may be reused
only when the inputs recorded next to it (clip window, caption style, source
fingerprint, transcript model) match what this run is asking for. Anything else
is rebuilt — a stale hit costs one JSON slice, a stale miss costs a wrong video.

The other half is the cache win: same window + same style + same source must
still skip crop_words/create_captions, and a resumed job must still not re-run
Whisper or pyannote. Those are asserted here too, because the tempting
"fix" (delete everything on every run) breaks them and costs an hour per job.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from utils import derived_cache

SOURCE_URL = "https://youtu.be/fake-job-110"

# The job-level transcript: what clip_0/words.json was wrongly cached from.
SOURCE_WORDS = [
    {"word": "Your", "start": 0.0, "end": 0.4},
    {"word": "phone", "start": 0.4, "end": 0.9},
]

STALE_WINDOW = (0.0, 30.0)          # what the 06:28 run cropped
FRESH_WINDOW = (3175.0, 3202.0)     # what the 20:45 run cut


class StopAfterCaptions(Exception):
    """Raised by the fake render step so one clip ends right after 5d."""


@pytest.fixture
def job_dir(tmp_path, monkeypatch):
    """A job directory in the shape the pipeline leaves behind on disk."""
    d = tmp_path / "job110"
    d.mkdir()
    (d / "source.mp4").write_bytes(b"a source, any source")
    (d / "source.urlhash").write_text(
        worker._url_fingerprint(SOURCE_URL), encoding="utf-8")
    (d / "words.json").write_text(json.dumps(SOURCE_WORDS), encoding="utf-8")
    # Keep the clip on the sequential path and off the model calls: this file
    # tests cache decisions, not ffmpeg and not the reframe heuristics.
    monkeypatch.setattr(worker.settings, "SINGLE_PASS_RENDER", False, raising=False)
    monkeypatch.setattr(worker.settings, "SCENE_ATTENTION_ENABLED", False, raising=False)
    monkeypatch.setattr(worker.settings, "DELETE_AUDIO_AFTER_TRANSCRIPTION", False, raising=False)
    return d


def _run_clip(job_dir, monkeypatch, calls, index=0, start=FRESH_WINDOW[0],
              end=FRESH_WINDOW[1], style="mrbeast"):
    """Drive worker.process_clip up to the render, recording the expensive steps.

    Everything that would shell out to ffmpeg is replaced by a recorder that
    writes a plausible artifact, so the cache logic sees the same file states it
    would in production. The clip always ends in ``StopAfterCaptions`` — that is
    the point: the cut/crop/caption decisions have all been taken by then, and
    the recorded error proves we stopped where we meant to.
    """
    def fake_cut(source, s, e, output):
        calls["cut"].append((float(s), float(e)))
        with open(output, "wb") as f:
            f.write(b"short")
        return output

    def fake_crop(input_file, s, e, output_file):
        calls["crop"].append((float(s), float(e)))
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump([{"word": f"window-{s}-{e}", "start": 0.0, "end": 1.0}], f)
        return output_file

    def fake_captions(words_json, output_ass, **kwargs):
        calls["captions"].append(kwargs.get("style_name"))
        # What text the burned captions are actually built from — the pairing the
        # bug broke, recorded at the moment the call happens.
        with open(words_json, encoding="utf-8") as handle:
            cropped = json.load(handle)
        calls["captions_from"].append(cropped[0]["word"] if cropped else "")
        with open(output_ass, "w", encoding="utf-8") as f:
            f.write("; fake ass\n")
        return output_ass

    def fake_smart_crop(src, dst, **kwargs):
        with open(dst, "wb") as f:
            f.write(b"vertical")
        return dst

    def stop_burn(*args, **kwargs):
        raise StopAfterCaptions("stopped right after the caption step")

    async def fake_metadata(*args, **kwargs):
        return {"title": "A real title", "description": "d", "hashtags": ["#Shorts"]}

    monkeypatch.setattr(worker, "cut_clip", fake_cut)
    monkeypatch.setattr(worker, "crop_words", fake_crop)
    monkeypatch.setattr(worker, "create_captions", fake_captions)
    monkeypatch.setattr(worker, "smart_crop_video", fake_smart_crop)
    monkeypatch.setattr(worker, "burn_captions", stop_burn)
    monkeypatch.setattr(worker, "generate_metadata", fake_metadata)

    clip = {"start": start, "end": end, "title": "Robinhood Restricted GameStop Trades",
            "hook": "Wall Street did something illegal", "popup_cues": []}
    job = SimpleNamespace(default_layout_mode="single_speaker", default_caption_style=style,
                          smart_blur_enabled=False, style_prompt="", auto_upload=False,
                          target_clips=1)
    errors = []
    asyncio.run(worker.process_clip(
        None, job, 110, job_dir, index, clip, 1, errors,
        str(job_dir / "source.mp4"), str(job_dir / "words.json"),
        "source title", "source channel", "PUBLIC", lambda stage, pct: None))
    assert errors and isinstance(errors[0]["error"], str)
    assert "caption step" in errors[0]["error"], (
        f"clip aborted before the render: {errors[0]['error']}")
    return errors


def _new_calls():
    return {"cut": [], "crop": [], "captions": [], "captions_from": []}


def _clip_words(clip_dir):
    with open(os.path.join(str(clip_dir), "words.json"), encoding="utf-8") as f:
        return json.load(f)


def manifest_records_window(clip_dir, window):
    """The manifest next to the artifact records the window it was built for."""
    manifest = os.path.join(str(clip_dir), derived_cache.MANIFEST_NAME)
    if not os.path.exists(manifest):
        return False
    with open(manifest, encoding="utf-8") as f:
        data = json.load(f)
    entry = (data.get("artifacts") or {}).get("words.json") or {}
    recorded = (entry.get("inputs") or {}).get("window")
    return bool(recorded) and [float(v) for v in recorded] == [float(window[0]), float(window[1])]


# ---------------------------------------------------------------------------
# the job-110 field state: per-clip text cached under an index, no manifest
# ---------------------------------------------------------------------------

class TestStalePerClipTextIsNeverReused:
    def test_legacy_words_json_for_another_window_is_recropped(self, job_dir, monkeypatch):
        """Job 110 exactly: clip_0/words.json from 0-30 s, no manifest, new window.

        Before the fix this logged "Words already cropped" and burned the 0-30 s
        transcript into a picture cut at 3175 s.
        """
        clip_dir = job_dir / "clip_0"
        clip_dir.mkdir()
        (clip_dir / "words.json").write_text(
            json.dumps([{"word": "Your phone is great", "start": STALE_WINDOW[0],
                         "end": STALE_WINDOW[1] / 2}]),
            encoding="utf-8")

        calls = _new_calls()
        _run_clip(job_dir, monkeypatch, calls)

        assert calls["crop"] == [FRESH_WINDOW], (
            "a words.json with no recorded window must never be treated as "
            f"this run's transcript; crop_words calls: {calls['crop']}")
        assert _clip_words(clip_dir)[0]["word"] == f"window-{FRESH_WINDOW[0]}-{FRESH_WINDOW[1]}"

    def test_a_fresh_cut_never_ships_the_previous_windows_captions(self, job_dir, monkeypatch):
        """Same field state, but watching the pairing instead of the call count:
        whatever the caption step was handed must be this window's text."""
        clip_dir = job_dir / "clip_0"
        clip_dir.mkdir()
        (clip_dir / "words.json").write_text(
            json.dumps([{"word": "own. Something the legacy", "start": 0.0, "end": 12.0}]),
            encoding="utf-8")

        calls = _new_calls()
        _run_clip(job_dir, monkeypatch, calls)

        assert calls["cut"] == [FRESH_WINDOW], "the picture is the new window"
        assert calls["captions_from"] == [f"window-{FRESH_WINDOW[0]}-{FRESH_WINDOW[1]}"], (
            "captions were built from a different part of the video than the cut: "
            f"{calls['captions_from']}")
        assert manifest_records_window(clip_dir, FRESH_WINDOW)
        words = _clip_words(clip_dir)
        assert words and all("Something the legacy" not in w["word"] for w in words), (
            f"captions for the new cut still come from the old window: {words}")

    def test_cleanup_kept_words_json_is_reused_for_the_same_window(self, job_dir, monkeypatch):
        """The steady state production actually leaves: cleanup deleted short.mp4
        and captions.ass, kept words.json. Same window => only the deleted two
        are rebuilt, the cached text is legitimately reused."""
        calls = _new_calls()
        _run_clip(job_dir, monkeypatch, calls)
        clip_dir = job_dir / "clip_0"
        for name in ("short.mp4", "vertical.mp4", "captions.ass", "final_short.mp4"):
            path = clip_dir / name
            if path.exists():
                path.unlink()  # exactly worker.py's keep_files cleanup

        second = _new_calls()
        _run_clip(job_dir, monkeypatch, second)

        assert second["crop"] == [], "same window: crop_words must stay skipped"
        assert second["captions"] == ["mrbeast"], "captions.ass was deleted, so rebuild it"
        assert second["cut"] == [FRESH_WINDOW], "short.mp4 was deleted, so re-cut it"


# ---------------------------------------------------------------------------
# keying: what invalidates, what does not
# ---------------------------------------------------------------------------

class TestCacheKeys:
    def test_same_window_and_style_skips_both_text_steps(self, job_dir, monkeypatch):
        calls = _new_calls()
        _run_clip(job_dir, monkeypatch, calls)
        second = _new_calls()
        _run_clip(job_dir, monkeypatch, second)

        assert second["crop"] == []
        assert second["captions"] == []
        assert second["cut"] == []
        assert calls["crop"] == [FRESH_WINDOW] and calls["captions"] == ["mrbeast"]

    def test_a_different_window_rebuilds_cut_words_and_captions(self, job_dir, monkeypatch):
        _run_clip(job_dir, monkeypatch, _new_calls())

        second = _new_calls()
        _run_clip(job_dir, monkeypatch, second, start=900.0, end=928.0)

        assert second["cut"] == [(900.0, 928.0)]
        assert second["crop"] == [(900.0, 928.0)]
        assert second["captions"] == ["mrbeast"]
        assert second["captions_from"] == ["window-900.0-928.0"]
        assert _clip_words(job_dir / "clip_0")[0]["word"] == "window-900.0-928.0"

    def test_a_different_caption_style_recaptions_but_keeps_the_words(self, job_dir, monkeypatch):
        """Style is part of the captions key only: the crop does not depend on it."""
        _run_clip(job_dir, monkeypatch, _new_calls(), style="mrbeast")

        second = _new_calls()
        _run_clip(job_dir, monkeypatch, second, style="hormozi")

        assert second["crop"] == [], "words.json does not depend on the caption style"
        assert second["captions"] == ["hormozi"]

    def test_two_clips_get_their_own_manifest_and_their_own_text(self, job_dir, monkeypatch):
        """clip_0 and clip_1 are different windows; an index-keyed cache made the
        second one inherit the first one's decisions, so each directory must
        carry its own key.
        """
        first = _new_calls()
        _run_clip(job_dir, monkeypatch, first, index=0, start=100.0, end=128.0)
        second = _new_calls()
        _run_clip(job_dir, monkeypatch, second, index=1, start=500.0, end=528.0)

        assert second["crop"] == [(500.0, 528.0)]
        assert _clip_words(job_dir / "clip_0")[0]["word"] == "window-100.0-128.0"
        assert _clip_words(job_dir / "clip_1")[0]["word"] == "window-500.0-528.0"
        assert manifest_records_window(job_dir / "clip_0", (100.0, 128.0))
        assert manifest_records_window(job_dir / "clip_1", (500.0, 528.0))


# ---------------------------------------------------------------------------
# stage_diarize: the resume ran before the gate and before the windows existed
# ---------------------------------------------------------------------------

def _diarize(job_dir, clips, monkeypatch=None, turns=None):
    calls = []

    def fake_windows(path, windows, margin=0.0):
        calls.append(list(windows))
        return turns if turns is not None else [
            {"speaker": f"SPEAKER_{n:02d}", "start": w["start"], "end": w["end"]}
            for n, w in enumerate(windows)]

    import services.diarization as diar
    if monkeypatch is not None:
        monkeypatch.setattr(diar, "try_diarize_windows", fake_windows)
    result = asyncio.run(worker.stage_diarize(
        110, job_dir, str(job_dir / "source.mp4"), clips, lambda stage, pct: None))
    return result, calls


class TestDiarizeResumeIsKeyedAndGated:
    def test_disabled_switch_beats_a_diarization_json_on_disk(self, job_dir, monkeypatch):
        """The file was written by an older run with the switch on. The switch
        is the operator's instruction; a leftover file is not."""
        (job_dir / "diarization.json").write_text(
            json.dumps({"turns": [{"speaker": "A", "start": 0.0, "end": 1.0}]}),
            encoding="utf-8")
        monkeypatch.setattr(worker.settings, "DIARIZATION_ENABLED", False)

        turns, _calls = _diarize(job_dir, [{"start": 10.0, "end": 40.0}], monkeypatch)

        assert turns == [], (
            "stage_diarize returned cached speaker turns while "
            "DIARIZATION_ENABLED=false — the resume runs before the gate")

    def test_resume_is_refused_when_the_windows_moved(self, job_dir, monkeypatch):
        _turns, first = _diarize(job_dir, [{"start": 10.0, "end": 40.0}], monkeypatch)
        assert first == [[{"start": 10.0, "end": 40.0}]]

        _second, calls = _diarize(job_dir, [{"start": 3175.0, "end": 3202.0}], monkeypatch)

        assert calls == [[{"start": 3175.0, "end": 3202.0}]], (
            "diarization over old clip windows was served for new ones")

    def test_identical_windows_still_resume_without_the_model(self, job_dir, monkeypatch):
        """The cache win pyannote pays for: 40+ min/hour of audio must not re-run
        when the selection is unchanged."""
        _diarize(job_dir, [{"start": 10.0, "end": 40.0}, {"start": 100.0, "end": 130.0}],
                 monkeypatch)

        def explode(path, windows, margin=0.0):
            raise AssertionError("same windows: diarization must not re-run")

        import services.diarization as diar
        monkeypatch.setattr(diar, "try_diarize_windows", explode)
        turns, _calls = _diarize(job_dir, [{"start": 10.0, "end": 40.0},
                                           {"start": 100.0, "end": 130.0}], monkeypatch=None)

        assert len(turns) == 2

    def test_per_clip_slice_returns_nothing_while_the_switch_is_off(self, job_dir, monkeypatch):
        (job_dir / "diarization.json").write_text(
            json.dumps({"turns": [{"speaker": "A", "start": 1.0, "end": 3.0}]}),
            encoding="utf-8")
        monkeypatch.setattr(worker.settings, "DIARIZATION_ENABLED", False)

        assert worker.load_clip_diarization(job_dir, 0.0, 10.0) == []


# ---------------------------------------------------------------------------
# job-level artifacts are keyed by the source fingerprint + transcript inputs
# ---------------------------------------------------------------------------

def _transcribe(job_dir, monkeypatch, calls, model="tiny", task=None, language=None):
    def fake_transcribe(audio_path, output_dir, mdl, **kwargs):
        calls.append((mdl, kwargs.get("task")))
        (job_dir / "segments.json").write_text(
            json.dumps([{"start": 0.0, "end": 1.0, "text": "hello"}]), encoding="utf-8")
        (job_dir / "words.json").write_text(
            json.dumps([{"word": "hello", "start": 0.0, "end": 0.5}]), encoding="utf-8")
        return str(job_dir / "segments.json"), str(job_dir / "words.json")

    monkeypatch.setattr(worker, "transcribe_audio_file", fake_transcribe)
    job = SimpleNamespace(whisper_model=model, transcription_task=task,
                          source_language=language, keep_native_transcript=False,
                          llm_translate=False, youtube_url=SOURCE_URL)
    return asyncio.run(worker.stage_transcribe(
        110, job, job_dir, str(job_dir / "audio.wav"), lambda stage, pct: None))


class TestTranscriptKeying:
    def test_legacy_transcript_on_disk_still_skips_whisper(self, job_dir, monkeypatch):
        """A pre-manifest words.json/segments.json is a 40-minute re-transcribe
        away, and the source cannot have changed under it (a re-download clears
        both). So this one stays existence-tolerant on purpose."""
        (job_dir / "segments.json").write_text(json.dumps([{"text": "old"}]), encoding="utf-8")
        (job_dir / "words.json").write_text(json.dumps(SOURCE_WORDS), encoding="utf-8")

        _transcribe(job_dir, monkeypatch, calls := [])

        assert calls == []

    def test_a_newer_run_with_the_same_inputs_skips_whisper(self, job_dir, monkeypatch):
        _transcribe(job_dir, monkeypatch, first := [])
        assert len(first) == 1

        _transcribe(job_dir, monkeypatch, second := [])

        assert second == [], "same source + same model must stay cached"

    def test_changing_the_whisper_model_rebuilds_the_transcript(self, job_dir, monkeypatch):
        _transcribe(job_dir, monkeypatch, [], model="tiny")

        _transcribe(job_dir, monkeypatch, second := [], model="large-v3")

        assert second == [("large-v3", "transcribe")], (
            "the transcript was cached from a different model than this run asked for")

    def test_a_new_source_clears_the_manifest_it_was_keyed_on(self, job_dir, monkeypatch):
        """_clear_derived_artifacts is the re-download path; it must also drop the
        manifest, or the next run trusts a key describing the previous video."""
        _transcribe(job_dir, monkeypatch, [], model="tiny")
        assert os.path.exists(str(job_dir / derived_cache.MANIFEST_NAME))

        worker._clear_derived_artifacts(job_dir, 110)

        assert not os.path.exists(str(job_dir / derived_cache.MANIFEST_NAME))
        assert not (job_dir / "words.json").exists()
        assert not (job_dir / "segments.json").exists()


# ---------------------------------------------------------------------------
# the manifest helper itself
# ---------------------------------------------------------------------------

class TestDerivedCacheManifest:
    def test_record_then_current_round_trips(self, tmp_path):
        art = tmp_path / "words.json"
        art.write_text("[]", encoding="utf-8")
        inputs = {"window": [0.0, 30.0], "style": "mrbeast"}

        assert derived_cache.record(str(art), "clip_words", inputs) is True
        assert derived_cache.is_current(str(art), "clip_words", inputs) is True

    def test_a_missing_artifact_is_never_current(self, tmp_path):
        art = tmp_path / "captions.ass"
        inputs = {"window": [0.0, 30.0]}
        derived_cache.record(str(art), "captions", inputs)  # nothing on disk yet

        assert derived_cache.is_current(str(art), "captions", inputs) is False

    def test_no_entry_means_stale_unless_legacy_is_allowed(self, tmp_path):
        art = tmp_path / "words.json"
        art.write_text("[]", encoding="utf-8")
        inputs = {"window": [0.0, 30.0]}

        assert derived_cache.is_current(str(art), "clip_words", inputs) is False
        assert derived_cache.is_current(str(art), "clip_words", inputs,
                                        legacy_current=True) is True

    def test_any_input_change_breaks_the_key(self, tmp_path):
        art = tmp_path / "words.json"
        art.write_text("[]", encoding="utf-8")
        base = {"window": [0.0, 30.0], "style": "mrbeast", "source": {"urlhash": "abc"}}
        derived_cache.record(str(art), "clip_words", base)

        assert derived_cache.is_current(str(art), "clip_words",
                                        {**base, "window": [10.0, 40.0]}) is False
        assert derived_cache.is_current(str(art), "clip_words",
                                        {**base, "style": "hormozi"}) is False
        assert derived_cache.is_current(str(art), "clip_words",
                                        {**base, "source": {"urlhash": "zzz"}}) is False
        assert derived_cache.is_current(str(art), "other_kind", base) is False

    def test_one_manifest_per_directory_holds_every_artifact(self, tmp_path):
        words = tmp_path / "words.json"
        captions = tmp_path / "captions.ass"
        words.write_text("[]", encoding="utf-8")
        captions.write_text(";", encoding="utf-8")

        derived_cache.record(str(words), "clip_words", {"window": [0.0, 30.0]})
        derived_cache.record(str(captions), "captions", {"window": [0.0, 30.0], "style": "x"})

        assert derived_cache.is_current(str(words), "clip_words", {"window": [0.0, 30.0]})
        assert derived_cache.is_current(str(captions), "captions",
                                        {"window": [0.0, 30.0], "style": "x"})
        # One manifest per directory, never a sidecar per artifact.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            derived_cache.MANIFEST_NAME, "captions.ass", "words.json"]

    def test_a_truncated_manifest_is_stale_not_fatal(self, tmp_path):
        art = tmp_path / "words.json"
        art.write_text("[]", encoding="utf-8")
        # The watchdog hard-exits a wedged worker; a half-written manifest is a
        # real state, and it must read as "rebuild", never as an exception.
        (tmp_path / derived_cache.MANIFEST_NAME).write_text('{"artifacts": {"words', encoding="utf-8")

        assert derived_cache.is_current(str(art), "clip_words", {"window": [0.0]}) is False
        assert derived_cache.record(str(art), "clip_words", {"window": [0.0]}) is True
        assert derived_cache.is_current(str(art), "clip_words", {"window": [0.0]}) is True

    def test_forget_drops_only_the_named_artifact(self, tmp_path):
        words = tmp_path / "words.json"
        captions = tmp_path / "captions.ass"
        words.write_text("[]", encoding="utf-8")
        captions.write_text(";", encoding="utf-8")
        derived_cache.record(str(words), "clip_words", {"window": [0.0]})
        derived_cache.record(str(captions), "captions", {"window": [0.0]})

        derived_cache.forget(str(words))

        assert not derived_cache.is_current(str(words), "clip_words", {"window": [0.0]})
        assert derived_cache.is_current(str(captions), "captions", {"window": [0.0]})

    def test_input_fingerprint_tracks_the_file_not_its_name(self, tmp_path):
        a = tmp_path / "words.json"
        a.write_text("[1]", encoding="utf-8")
        first = derived_cache.input_fingerprint(str(a))

        assert derived_cache.input_fingerprint(str(a)) == first
        a.write_text("[1, 2, 3]", encoding="utf-8")

        assert derived_cache.input_fingerprint(str(a)) != first
        assert derived_cache.input_fingerprint(str(tmp_path / "nope.json")) == {"absent": True}
