"""Re-rendering a clip: the blocking chain leaves the loop, and the trim survives it.

Two defects meet in ``POST /api/clips/{clip_id}/rerender``.

* Defect 4: the entire ffmpeg pipeline - ``cut_clip``, ``smart_crop_video``,
  ``create_captions``, ``burn_captions``, ``add_automatic_sfx``,
  ``generate_thumbnail`` - ran inline inside an ``async def``. A re-render is
  minutes of blocking subprocess work, and while it runs the single event loop
  cannot serve anything else: not the Studio, not ``/health``, not the worker's
  own API calls. The fix asked for is to move the blocking work to a thread, not
  to rebuild the endpoint as a job queue.

* Defect 1's other half: ``cut_clip(source_video, clip.start, clip.end, ...)``
  reads the clip *window* columns and ignores ``trim_start``/``trim_end``, and
  the cleanup pass at the end deletes every ``.mp4`` in the clip directory that
  is not ``final_video.mp4``. So a re-render silently undid the operator's trim
  and republished exactly what they had rejected.

What these tests hold: nothing blocking runs on the event loop; a recorded trim
is re-applied to the fresh render and the row points at it; a trim that no
longer fits the new render is reported instead of swallowed; and a render that
dies still answers 500.

No ffmpeg: every service the endpoint calls is stubbed.
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from main import app
from utils import clip_trim, derived_cache

RENDER_BYTES = b"FRESH-RENDER-OF-THE-WHOLE-WINDOW-0123456789"
TRIMMED_NAME = "final_video.trimmed.mp4"


def _thread():
    """``on-loop`` in the event loop's thread, ``off-loop`` in a worker thread.

    TestClient runs the app on a real loop, so ``get_running_loop()`` is a
    deterministic probe of which thread a call happened in.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "off-loop"
    return "on-loop"


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def pipeline(db, monkeypatch, client):
    """A completed job with a source file, and stubs for every render service.

    Each stub records the thread it ran in and writes its output, so the
    endpoint's file bookkeeping has something real to work with.
    """
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(Path(settings.JOBS_DIR).parent))

    job = Job(youtube_url="https://youtu.be/rerender", status="completed")
    db.add(job)
    db.commit()
    db.refresh(job)
    clip = Clip(job_id=job.id, index=0, start=100.0, end=130.0, title="Window",
                caption_style="mrbeast", layout_mode="single_speaker")
    db.add(clip)
    db.commit()
    db.refresh(clip)

    job_dir = Path(settings.JOBS_DIR) / str(job.id)
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    clip_dir = job_dir / f"clip_{clip.index}"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "source.mp4").write_bytes(b"SOURCE")
    (job_dir / "words.json").write_text('{"words": []}', encoding="utf-8")

    calls = []

    def stub(name, output_index):
        def _call(*args, **kwargs):
            calls.append({"step": name, "thread": _thread(), "args": args})
            out = Path(args[output_index])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(RENDER_BYTES)
        return _call

    import worker
    import services.smart_crop as smart_crop
    import services.word_cropper as word_cropper
    import services.captions as captions_service
    import services.render as render_service
    import services.motion_title as motion_title_service
    import services.sfx as sfx_service
    import services.thumbnail as thumbnail_service
    import services.trim as trim_service

    monkeypatch.setattr(worker, "cut_clip", stub("cut_clip", 3))
    monkeypatch.setattr(smart_crop, "smart_crop_video", stub("smart_crop", 1))
    monkeypatch.setattr(word_cropper, "crop_words", stub("crop_words", 3))
    monkeypatch.setattr(captions_service, "create_captions", stub("create_captions", 1))
    monkeypatch.setattr(render_service, "burn_captions", stub("burn_captions", 2))
    monkeypatch.setattr(motion_title_service, "add_motion_title", stub("add_motion_title", 2))
    monkeypatch.setattr(sfx_service, "add_automatic_sfx", stub("add_automatic_sfx", 1))
    monkeypatch.setattr(thumbnail_service, "generate_thumbnail", stub("generate_thumbnail", 1))

    def fake_trim(source, start, end, output, **kwargs):
        calls.append({"step": "trim_clip", "thread": _thread(),
                      "args": (source, start, end, output)})
        base = Path(source).read_bytes()
        if end > len(base):
            from services.trim import TrimValidationError
            raise TrimValidationError(f"end {end}s is past the end of the media")
        Path(output).write_bytes(f"TRIMMED[{start}-{end}]".encode() + base)
        return {"output_path": output, "duration": end - start,
                "method": "stream_copy", "used_fallback": False}

    monkeypatch.setattr(trim_service, "trim_clip", fake_trim)

    def rerender(clip_id=None, **payload):
        body = {"caption_style": "mrbeast", "layout_mode": "single_speaker",
                "motion_title": False, "karaoke": False}
        body.update(payload)
        return client.post(f"/api/clips/{clip_id or clip.id}/rerender", json=body)

    return {"job": job, "clip": clip, "clip_dir": clip_dir, "job_dir": job_dir,
            "calls": calls, "rerender": rerender}


def _on_loop(pipeline):
    return sorted({c["step"] for c in pipeline["calls"] if c["thread"] == "on-loop"})


# ---------------------------------------------------------------------------
# defect 4: no blocking work on the event loop
# ---------------------------------------------------------------------------

def test_the_render_chain_does_not_run_on_the_event_loop(pipeline):
    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    assert pipeline["calls"], "the stubbed render services were never called"
    assert not _on_loop(pipeline), (
        f"{_on_loop(pipeline)} ran on the event loop: one re-render blocks every "
        "other request the service answers, including /health")


def test_the_trim_reapply_also_runs_off_the_event_loop(pipeline, db):
    clip = pipeline["clip"]
    clip.trim_start, clip.trim_end = 5.0, 20.0
    db.commit()

    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    assert not _on_loop(pipeline)


def test_a_failed_render_is_still_a_500(pipeline, monkeypatch):
    """No silent fallback: a render that dies is an error, not a success that
    quietly keeps serving the previous file."""
    import services.render as render_service

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg exited 1")

    monkeypatch.setattr(render_service, "burn_captions", boom)

    response = pipeline["rerender"]()

    assert response.status_code == 500, response.text
    assert "Re-render failed" in response.json()["detail"]


# ---------------------------------------------------------------------------
# defect 1, re-render half: the trim is not lost
# ---------------------------------------------------------------------------

def test_a_rerender_reapplies_the_recorded_trim(pipeline, db):
    clip = pipeline["clip"]
    clip.trim_start, clip.trim_end = 5.0, 20.0
    db.commit()

    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    trimmed = pipeline["clip_dir"] / TRIMMED_NAME
    assert trimmed.exists(), (
        "the re-render wrote a fresh final_video.mp4 and dropped the operator's "
        "trim: the short that publishes is the one they rejected")
    assert trimmed.read_bytes().startswith(b"TRIMMED[5.0-20.0]")
    db.refresh(clip)
    assert clip.file_path.endswith(TRIMMED_NAME), (
        f"the row serves {clip.file_path}, not the trim it records")


def test_the_fresh_trim_is_keyed_to_the_new_render(pipeline, db):
    clip = pipeline["clip"]
    clip.trim_start, clip.trim_end = 5.0, 20.0
    db.commit()
    # An artifact left over from the *previous* render, with a matching key.
    render = pipeline["clip_dir"] / "final_video.mp4"
    render.write_bytes(b"OLD-RENDER")
    trimmed = pipeline["clip_dir"] / TRIMMED_NAME
    trimmed.write_bytes(b"OLD-TRIM")
    derived_cache.record(str(trimmed), clip_trim.TRIM_KIND,
                         clip_trim.trim_inputs(str(render), (clip.start, clip.end),
                                               5.0, 20.0))
    assert clip_trim.is_trim_current(pipeline["clip_dir"], (clip.start, clip.end),
                                     5.0, 20.0)

    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    entry = json.loads(Path(derived_cache.manifest_path(str(trimmed))).read_text(
        encoding="utf-8"))["artifacts"][TRIMMED_NAME]
    assert entry["inputs"]["base"]["size"] == render.stat().st_size, (
        "the surviving trim still vouches for the render that was just replaced")
    assert trimmed.read_bytes().startswith(b"TRIMMED[5.0-20.0]")


def test_a_rerender_with_no_recorded_trim_leaves_no_trimmed_artifact(pipeline, db):
    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    assert not (pipeline["clip_dir"] / TRIMMED_NAME).exists()
    db.refresh(pipeline["clip"])
    assert pipeline["clip"].file_path.endswith("final_video.mp4")


def test_a_stale_trimmed_artifact_is_cleaned_up_by_the_render(pipeline, db):
    """The cleanup pass deletes non-final ``.mp4`` files. With no trim recorded,
    an artifact from an earlier render must not stay on disk pointing at this row."""
    stale = pipeline["clip_dir"] / TRIMMED_NAME
    stale.write_bytes(b"TRIM-OF-A-DEAD-RENDER")

    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    assert not stale.exists()


def test_a_trim_that_no_longer_fits_the_new_render_is_reported(pipeline, db, monkeypatch):
    """The new render came out shorter than the recorded window. Falling back to
    the untrimmed file is a silent downgrade; the operator has to be told."""
    import services.trim as trim_service
    from services.trim import TrimValidationError

    clip = pipeline["clip"]
    clip.trim_start, clip.trim_end = 0.0, 9999.0
    db.commit()

    def refusing(source, start, end, output, **kwargs):
        pipeline["calls"].append({"step": "trim_clip", "thread": _thread(),
                                  "args": (source, start, end, output)})
        raise TrimValidationError("end 9999.0s is past the end of the media")

    monkeypatch.setattr(trim_service, "trim_clip", refusing)

    response = pipeline["rerender"]()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trim_reapplied"] is False, (
        "the response claims the trim was re-applied when it was refused")
    assert "past the end" in str(body.get("trim_error") or ""), (
        "the trim was dropped without a word; the Studio still shows 0-9999 while "
        "the clip publishes whole")
    db.refresh(clip)
    assert clip.file_path.endswith("final_video.mp4")
