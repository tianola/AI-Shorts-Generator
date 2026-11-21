"""A manual trim is a derived artifact, keyed by the window it was cut from.

`POST /api/clips/{id}/trim` cut the window and then ``os.replace``-d the result
over ``clip_dir/final_video.mp4``. Three things followed from that:

* The render the operator approved is gone. Nothing can rebuild it except a
  full re-render, and a re-render is exactly what the trim's own docstring
  assumes will happen ("the UI serves the trimmed version").
* A second trim of the same clip cuts the *already trimmed* file, so the
  windows compound: trim 5-20 s, then trim 0-10 s, and the clip is 5-15 s of
  the original with no record anywhere of that having happened.
* The file's own identity no longer matches anything: ``trim_start``/``trim_end``
  are on the row, but the manifest key of ``final_video.mp4`` (when there is one)
  describes the untrimmed cut. That is the stale-artifact class of bug
  ``utils/derived_cache.py`` exists to close - "an artifact whose key does not
  mention the thing that changed".

The contract these tests hold: the pristine render is never overwritten, the
trim lives beside it under its own key, re-trimming always starts from the
render, and a trim whose base changed is rebuilt rather than trusted.

No ffmpeg here: the trim service is stubbed, and the assertions are about the
files and the cache keys the endpoint leaves behind.
"""
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
from utils import derived_cache

BASE_RENDER = b"PRISTINE-RENDER-OF-THE-WHOLE-GRID-WINDOW"
BLUR_BYTES = b"BLURRED-RENDER-OF-THE-WHOLE-GRID-WINDOW"
TRIMMED_NAME = "final_video.trimmed.mp4"


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
def rendered(db, monkeypatch):
    """A completed job whose clip 0 has a rendered final_video.mp4 on disk, plus a
    stubbed trim service that records what it was asked to cut."""
    # STORAGE_ROOT becomes an ancestor of the temp jobs tree, so the relative
    # paths the endpoint stores resolve back to the files it wrote.
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(Path(settings.JOBS_DIR).parent))

    job = Job(youtube_url="https://youtu.be/trimme", status="completed")
    db.add(job)
    db.commit()
    db.refresh(job)
    clip = Clip(job_id=job.id, index=0, start=3175.0, end=3202.0, title="Grid window",
                file_path=None)
    db.add(clip)
    db.commit()
    db.refresh(clip)

    # JOBS_DIR is a fixed per-session directory shared by every test in this file,
    # so the clip directory is wiped rather than reused: no leftover artifact from
    # a previous test may vouch for the one now running.
    clip_dir = Path(settings.JOBS_DIR) / str(job.id) / f"clip_{clip.index}"
    if clip_dir.exists():
        shutil.rmtree(clip_dir)
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "final_video.mp4").write_bytes(BASE_RENDER)
    clip.file_path = os.path.relpath(str(clip_dir / "final_video.mp4"), settings.STORAGE_ROOT)
    db.commit()

    calls = []

    def fake_trim(source, start, end, output, **kwargs):
        calls.append({"source": source, "start": start, "end": end, "output": output,
                      "source_bytes": _read(source),
                      "force_reencode": kwargs.get("force_reencode", False)})
        Path(output).write_bytes(f"TRIMMED[{start}-{end}]".encode())
        return {"output_path": output, "duration": end - start,
                "method": "stream_copy", "used_fallback": False}

    import services.trim as trim_service

    monkeypatch.setattr(trim_service, "trim_clip", fake_trim)
    return {"job": job, "clip": clip, "clip_dir": clip_dir, "calls": calls}


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _trim(client, clip_id, start, end):
    return client.post(f"/api/clips/{clip_id}/trim", json={"start": start, "end": end})


def manifest_entry(path):
    manifest = Path(derived_cache.manifest_path(path))
    if not manifest.exists():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return (data.get("artifacts") or {}).get(os.path.basename(str(path)))


# ---------------------------------------------------------------------------
# the render survives
# ---------------------------------------------------------------------------

def test_trimming_leaves_the_pristine_render_alone(rendered, client, db):
    response = _trim(client, rendered["clip"].id, 5.0, 20.0)

    assert response.status_code == 200, response.text
    base = rendered["clip_dir"] / "final_video.mp4"
    assert base.read_bytes() == BASE_RENDER, (
        "the endpoint overwrote the render it was given: nothing can rebuild it "
        "now, and every later re-render starts from a file the operator trimmed")


def test_the_trim_lives_in_its_own_artifact_and_the_row_points_at_it(rendered, client, db):
    clip = rendered["clip"]
    response = _trim(client, clip.id, 5.0, 20.0)

    assert response.status_code == 200, response.text
    trimmed = rendered["clip_dir"] / TRIMMED_NAME
    assert trimmed.exists(), "the trimmed cut has no file of its own"
    assert trimmed.read_bytes() == b"TRIMMED[5.0-20.0]"
    db.refresh(clip)
    assert clip.file_path.endswith(TRIMMED_NAME), (
        f"the row still serves final_video.mp4 ({clip.file_path}); the operator's "
        "trim is not what the UI and the publish path read")
    assert (clip.trim_start, clip.trim_end) == (5.0, 20.0)


def test_the_trim_is_keyed_by_its_window_and_its_base(rendered, client, db):
    clip = rendered["clip"]
    _trim(client, clip.id, 5.0, 20.0)

    trimmed = rendered["clip_dir"] / TRIMMED_NAME
    entry = manifest_entry(trimmed)
    assert entry, ("the trimmed file carries no derived-cache entry, so any later "
                   "run has to take its word for what it is")
    assert entry["kind"] == "clip_final_trimmed"
    inputs = entry["inputs"]
    assert inputs["trim"] == [5.0, 20.0]
    assert inputs["clip_window"] == [clip.start, clip.end]
    assert inputs["base"]["name"] == "final_video.mp4"
    assert inputs["base"]["size"] == len(BASE_RENDER)
    assert derived_cache.is_current(str(trimmed), "clip_final_trimmed", inputs)


def test_re_running_the_same_trim_does_not_cut_again(rendered, client, db):
    """The cache win: same window, same base, no ffmpeg call the second time."""
    clip = rendered["clip"]
    first = _trim(client, clip.id, 5.0, 20.0)
    assert first.status_code == 200, first.text
    assert len(rendered["calls"]) == 1

    again = _trim(client, clip.id, 5.0, 20.0)

    assert again.status_code == 200, again.text
    assert again.json()["method"] == "cached"
    assert len(rendered["calls"]) == 1, "the identical trim was cut twice"


# ---------------------------------------------------------------------------
# re-trimming never compounds
# ---------------------------------------------------------------------------

def test_a_second_trim_starts_from_the_render_not_from_the_previous_trim(rendered, client, db):
    clip = rendered["clip"]
    _trim(client, clip.id, 5.0, 20.0)
    rendered["calls"].clear()

    response = _trim(client, clip.id, 0.0, 10.0)

    assert response.status_code == 200, response.text
    call = rendered["calls"][-1]
    assert os.path.basename(call["source"]) == "final_video.mp4", (
        f"a trim of a trim: the second cut was taken from {os.path.basename(call['source'])}")
    assert call["source_bytes"] == BASE_RENDER, (
        f"the second cut was taken from {call['output']}, whose contents are the first "
        "trim's output: the windows compound, so the clip is 5-15 s of the original "
        "while the row claims 0-10 s of it")
    db.refresh(clip)
    assert (clip.trim_start, clip.trim_end) == (0.0, 10.0)
    entry = manifest_entry(rendered["clip_dir"] / TRIMMED_NAME)
    assert entry["inputs"]["trim"] == [0.0, 10.0]


def test_a_replaced_render_invalidates_the_trimmed_artifact(rendered, client, db):
    """The base render is part of the key. A re-render that writes a new file must
    not leave the old trim looking current - that is the stale artifact the
    manifest module was written for."""
    from utils import clip_trim

    clip = rendered["clip"]
    window = (clip.start, clip.end)
    _trim(client, clip.id, 5.0, 20.0)
    assert clip_trim.is_trim_current(rendered["clip_dir"], window, 5.0, 20.0)

    (rendered["clip_dir"] / "final_video.mp4").write_bytes(BASE_RENDER * 3)

    assert not clip_trim.is_trim_current(rendered["clip_dir"], window, 5.0, 20.0), (
        "a trim cut from the previous render still vouches for itself after the "
        "render it came from was replaced")

    # And the endpoint acts on that: the same window is re-cut, not served cached.
    rendered["calls"].clear()
    response = _trim(client, clip.id, 5.0, 20.0)

    assert response.status_code == 200, response.text
    assert response.json()["method"] != "cached"
    assert len(rendered["calls"]) == 1, (
        "the stale trimmed file was served for a render it was never cut from")
    assert rendered["calls"][-1]["source"].endswith("final_video.mp4")


# ---------------------------------------------------------------------------
# validation stays at the boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start,end", [(20.0, 20.0), (30.0, 10.0), (-1.0, 5.0)])
def test_an_impossible_trim_is_refused_and_touches_nothing(rendered, client, db, start, end):
    clip = rendered["clip"]
    before = (rendered["clip_dir"] / "final_video.mp4").read_bytes()

    response = _trim(client, clip.id, start, end)

    assert response.status_code == 422, response.text
    assert (rendered["clip_dir"] / "final_video.mp4").read_bytes() == before
    assert not (rendered["clip_dir"] / TRIMMED_NAME).exists()
    db.refresh(clip)
    assert clip.trim_start is None and clip.trim_end is None
    assert not clip.file_path.endswith(TRIMMED_NAME)


def test_a_nan_trim_window_is_refused(rendered, client, db):
    """``float("nan")`` parses and every comparison with it is False, so the
    endpoint's own ``end <= start`` guard cannot see it."""
    response = _trim(client, rendered["clip"].id, "NaN", 10.0)

    assert response.status_code == 422, response.text
    assert rendered["calls"] == [], "the trim service was called with a NaN window"


def test_probing_a_trimmed_clip_reports_the_render_the_window_indexes(rendered, client,
                                                                       db, monkeypatch):
    """``/probe`` is what the operator reads to know how far they may trim, and a
    trim window is relative to the pristine render, not to the trimmed cut."""
    import services.trim as trim_service

    def fake_probe(path):
        return {"duration": 27.0, "has_video": True, "has_audio": True,
                "video_codec": "h264", "audio_codec": "aac", "width": 1080, "height": 1920}

    monkeypatch.setattr(trim_service, "probe_media", fake_probe)

    _trim(client, rendered["clip"].id, 5.0, 20.0)
    response = client.get(f"/api/clips/{rendered['clip'].id}/probe")

    assert response.status_code == 200, response.text
    body = response.json()
    assert Path(body["media"]).name == "final_video.mp4"
    assert body["trimmed_media"].endswith(TRIMMED_NAME)


# ---------------------------------------------------------------------------
# anything that rewrites the render owes the trim a rebuild
# ---------------------------------------------------------------------------

@pytest.fixture
def blurred(rendered, client, db, monkeypatch):
    """POST /smart-blur with the YOLO pass and the render stubbed out.

    Smart blur replaces ``final_video.mp4`` wholesale, so it is the other place
    where a recorded trim can be quietly thrown away.
    """
    import asyncio
    import services.focus_tracking as focus
    import services.render as render_service
    import services.vertical as vertical_service

    def _thread():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return "off-loop"
        return "on-loop"

    monkeypatch.setattr(focus, "get_smart_blur_filter_for_video",
                        lambda media, classes=None: "[blur filter]")
    monkeypatch.setattr(vertical_service, "build_vertical_filter",
                        lambda media, mode="static": "[vertical filter]")

    def fake_render(source, start, end, output, **kwargs):
        rendered["calls"].append({"source": source, "start": start, "end": end,
                                  "output": output, "thread": _thread(),
                                  "source_bytes": _read(source)})
        Path(output).write_bytes(BLUR_BYTES)

    monkeypatch.setattr(render_service, "render_single_pass", fake_render)
    monkeypatch.setattr(focus, "unsafe_class_ids", lambda: [45])
    import api.features as features_module
    monkeypatch.setattr(features_module, "_probe_duration", lambda path: 27.0)
    return rendered


def _blur(client, clip_id):
    return client.post(f"/api/clips/{clip_id}/smart-blur")


def test_a_smart_blur_keeps_the_recorded_trim(blurred, client, db):
    clip = blurred["clip"]
    _trim(client, clip.id, 5.0, 20.0)
    blurred["calls"].clear()

    response = _blur(client, clip.id)

    assert response.status_code == 200, response.text
    assert (blurred["clip_dir"] / "final_video.mp4").read_bytes() == BLUR_BYTES
    trimmed = blurred["clip_dir"] / TRIMMED_NAME
    assert trimmed.exists(), "the blur left no trimmed file: the row serves nothing"
    assert trimmed.read_bytes().startswith(b"TRIMMED[5.0-20.0]"), (
        "the blur changed the render but not the trim cut sitting beside it, so "
        "the clip publishes the pre-blur frames the operator had covered")
    trim_call = [c for c in blurred["calls"]
                 if str(c["output"]).endswith(TRIMMED_NAME)][-1]
    assert trim_call["source_bytes"] == BLUR_BYTES, (
        "the re-applied trim was cut from something other than the blurred render")
    db.refresh(clip)
    assert clip.file_path.endswith(TRIMMED_NAME)


def test_a_smart_blur_reports_what_it_did_with_the_trim(blurred, client, db):
    clip = blurred["clip"]
    _trim(client, clip.id, 5.0, 20.0)

    response = _blur(client, clip.id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["blurred"] is True
    assert body["trim_reapplied"] is True
    assert body["file_path"].endswith(TRIMMED_NAME)


def test_the_blur_render_does_not_run_on_the_event_loop(blurred, client, db):
    """Detection and the re-render are both subprocess work, and this endpoint
    is one operator click away from stalling the whole service."""
    _trim(client, blurred["clip"].id, 5.0, 20.0)

    response = _blur(client, blurred["clip"].id)

    assert response.status_code == 200, response.text
    blocking = [c for c in blurred["calls"] if "thread" in c]
    assert blocking, "the blur render stub was never called"
    assert all(c["thread"] == "off-loop" for c in blocking), (
        "the blur re-render ran on the event loop")
