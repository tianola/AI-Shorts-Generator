"""GET /api/clips/{id}/edit and POST /api/clips/{id}/ab-check (api/editing.py).

Each test builds a real Job and Clip row and writes the files a finished clip leaves in
storage (edit_timeline.json, final_video.mp4, final_plain.mp4, ab_report.json). The media files
are placeholder bytes: the A/B analysis itself is replaced by a fake, so no ffmpeg runs here.
Nothing calls a real LLM.
"""
import json
import os
import sys
from pathlib import Path

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import pytest
from fastapi.testclient import TestClient

from config import settings
from database.base import Base
from database.models import Clip, Job
from database.session import SessionLocal, engine
from main import app
from services.editing import ab
from services.editing.models import Event, Timeline, VisualTrackItem, timeline_to_json

Base.metadata.create_all(bind=engine)

FINAL = "final_video.mp4"
PLAIN = "final_plain.mp4"
TIMELINE = "edit_timeline.json"


def _timeline(with_visual=True):
    events = [Event(id="e1", kind="hook", start=1.0, end=3.0, intensity=0.9, priority=5,
                    source="words", reason="Opens on a question")]
    visual = ([VisualTrackItem(kind="zoom_punch", start=1.0, end=1.5, intensity=0.8, event_id="e1")]
              if with_visual else [])
    return Timeline(clip_duration=10.0, profile="standard", tone="neutral", events=events,
                    visual_tracks=visual)


@pytest.fixture()
def storage(monkeypatch):
    """Serve media from the test jobs folder: the /media paths are relative to its parent."""
    jobs = settings.JOBS_DIR
    monkeypatch.setattr(settings, "STORAGE_ROOT", os.path.dirname(jobs))
    return Path(jobs)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_clip(db, storage, *, timeline=None, files=(), quality=None, manifest=None,
               edit_profile="standard", tone="neutral"):
    """A job and one clip. `timeline` is a Timeline, a raw string written as-is, or None.
    `files` lists the media/report files to create next to it."""
    job = Job(youtube_url="https://example.invalid/watch?v=test")
    db.add(job)
    db.commit()
    db.refresh(job)
    clip = Clip(job_id=job.id, index=1, start=0.0, end=10.0, edit_profile=edit_profile,
                tone_category=tone, quality_report=quality, render_manifest=manifest,
                render_fingerprint="abc123" if manifest else None)
    db.add(clip)
    db.commit()
    db.refresh(clip)

    clip_dir = storage / str(job.id) / f"clip_{clip.index}"
    clip_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(timeline, Timeline):
        (clip_dir / TIMELINE).write_text(timeline_to_json(timeline), encoding="utf-8")
    elif isinstance(timeline, str):
        (clip_dir / TIMELINE).write_text(timeline, encoding="utf-8")
    for name in files:
        if name.endswith(".json"):
            (clip_dir / name).write_text(json.dumps({"items": [], "control": None, "speed": 1.0}),
                                         encoding="utf-8")
        else:
            (clip_dir / name).write_bytes(b"placeholder")
    return clip


def _prefix(storage, clip):
    return f"{storage.name}/{clip.job_id}/clip_{clip.index}"


# ------------------------------------------------------------------ GET /edit

def test_unknown_clip_is_404(client):
    response = client.get("/api/clips/999999999/edit")
    assert response.status_code == 404


def test_clip_without_edit_files_still_answers(client, db, storage):
    clip = _make_clip(db, storage)
    response = client.get(f"/api/clips/{clip.id}/edit")
    assert response.status_code == 200
    body = response.json()
    assert body["timeline"] is None
    assert body["explanations"] == []
    assert body["final_url"] is None
    assert body["plain_url"] is None
    assert body["ab"] is None
    assert body["speed"] == 1.0
    assert body["profile"] == "standard"
    assert body["tone"] == "neutral"


def test_full_record_returns_timeline_urls_and_ab(client, db, storage):
    report = {"control": {"time_clip": 8.0, "ssim": 0.999, "psnr": 50.0}, "speed": 1.0,
              "items": [{"kind": "zoom_punch", "visible": True}]}
    clip = _make_clip(db, storage, timeline=_timeline(), files=(FINAL, PLAIN, ab.AB_FILE),
                      quality={"status": "passed", "findings": []})
    (storage / str(clip.job_id) / f"clip_{clip.index}" / ab.AB_FILE).write_text(
        json.dumps(report), encoding="utf-8")

    body = client.get(f"/api/clips/{clip.id}/edit").json()
    assert body["timeline"]["profile"] == "standard"
    assert body["timeline"]["visual_tracks"][0]["kind"] == "zoom_punch"
    assert body["final_url"] == f"{_prefix(storage, clip)}/{FINAL}"
    assert body["plain_url"] == f"{_prefix(storage, clip)}/{PLAIN}"
    assert body["ab"] == report
    assert body["quality"] == {"status": "passed", "findings": []}
    assert isinstance(body["explanations"], list)


def test_missing_final_file_gives_no_final_url(client, db, storage):
    clip = _make_clip(db, storage, timeline=_timeline(), files=(PLAIN,))
    body = client.get(f"/api/clips/{clip.id}/edit").json()
    assert body["final_url"] is None
    assert body["plain_url"] == f"{_prefix(storage, clip)}/{PLAIN}"


def test_speed_comes_from_an_applied_anti_fingerprint_step(client, db, storage):
    applied = _make_clip(db, storage, manifest={"anti_fingerprint": {"applied": True, "speed": 1.02}})
    assert client.get(f"/api/clips/{applied.id}/edit").json()["speed"] == 1.02


def test_speed_is_one_when_the_step_was_not_applied(client, db, storage):
    clip = _make_clip(db, storage, manifest={"anti_fingerprint": {"applied": False, "speed": 1.02}})
    assert client.get(f"/api/clips/{clip.id}/edit").json()["speed"] == 1.0


@pytest.mark.parametrize("raw", ["{not json", "[1, 2, 3]", ""])
def test_unreadable_or_non_object_timeline_is_treated_as_missing(client, db, storage, raw):
    clip = _make_clip(db, storage, timeline=raw)
    response = client.get(f"/api/clips/{clip.id}/edit")
    assert response.status_code == 200
    body = response.json()
    assert body["timeline"] is None
    assert body["explanations"] == []


# ------------------------------------------------------------ POST /ab-check

def test_ab_check_unknown_clip_is_404(client):
    assert client.post("/api/clips/999999999/ab-check").status_code == 404


def test_ab_check_without_timeline_is_409(client, db, storage):
    clip = _make_clip(db, storage, files=(FINAL, PLAIN))
    response = client.post(f"/api/clips/{clip.id}/ab-check")
    assert response.status_code == 409
    assert "timeline" in response.json()["detail"]


def test_ab_check_without_plain_copy_is_409(client, db, storage):
    clip = _make_clip(db, storage, timeline=_timeline(), files=(FINAL,))
    response = client.post(f"/api/clips/{clip.id}/ab-check")
    assert response.status_code == 409
    assert "RENDER_PLAIN_PREVIEW" in response.json()["detail"]


def test_ab_check_without_finished_file_is_409(client, db, storage):
    clip = _make_clip(db, storage, timeline=_timeline(), files=(PLAIN,))
    assert client.post(f"/api/clips/{clip.id}/ab-check").status_code == 409


def test_ab_check_without_visual_effects_is_409(client, db, storage):
    clip = _make_clip(db, storage, timeline=_timeline(with_visual=False), files=(FINAL, PLAIN))
    response = client.post(f"/api/clips/{clip.id}/ab-check")
    assert response.status_code == 409
    assert "no visual effects" in response.json()["detail"]


def test_ab_check_runs_the_comparison_and_saves_the_report(client, db, storage, monkeypatch):
    calls = {}

    def fake_report(plain, final, tracks, duration, speed, work_dir):
        calls.update(plain=plain, final=final, tracks=tracks, duration=duration, speed=speed)
        return {"control": None, "speed": speed,
                "items": [{"kind": "zoom_punch", "visible": True}]}

    monkeypatch.setattr(ab, "ab_report", fake_report)
    clip = _make_clip(db, storage, timeline=_timeline(), files=(FINAL, PLAIN),
                      manifest={"anti_fingerprint": {"applied": True, "speed": 1.02}})

    response = client.post(f"/api/clips/{clip.id}/ab-check")
    assert response.status_code == 200
    assert response.json()["items"] == [{"kind": "zoom_punch", "visible": True}]
    assert response.json()["speed"] == 1.02

    clip_dir = storage / str(clip.job_id) / f"clip_{clip.index}"
    assert calls["plain"].endswith(PLAIN)
    assert calls["final"].endswith(FINAL)
    assert calls["duration"] == 10.0
    assert calls["speed"] == 1.02
    assert calls["tracks"][0]["kind"] == "zoom_punch"
    saved = json.loads((clip_dir / ab.AB_FILE).read_text(encoding="utf-8"))
    assert saved["items"] == [{"kind": "zoom_punch", "visible": True}]


def test_ab_check_failure_answers_500_and_saves_nothing(storage, db, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(ab, "ab_report", broken)
    clip = _make_clip(db, storage, timeline=_timeline(), files=(FINAL, PLAIN))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(f"/api/clips/{clip.id}/ab-check")
    assert response.status_code == 500
    assert not (storage / str(clip.job_id) / f"clip_{clip.index}" / ab.AB_FILE).exists()
