"""Cutting a clip by its words, dropping a cut, and the render switches the studio remembers.

Everything runs against the in-memory database from conftest and a temporary jobs folder.
No ffmpeg run is needed: the endpoints are called directly with a dummy render file.
"""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from api import features
from api.common import clip_payload
from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database


@pytest.fixture
def db(monkeypatch):
    # conftest points settings.JOBS_DIR at a temp folder for the whole session. The storage root
    # is put next to it, so paths relative to it stay on the same drive.
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(Path(settings.JOBS_DIR).parent), raising=False)
    init_database()
    session = SessionLocal()
    made = []
    yield session, made
    for folder in made:
        shutil.rmtree(folder, ignore_errors=True)
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _clip_with_render(db_and_made, start=100.0, end=110.0):
    db, made = db_and_made
    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=False)
    db.add(job)
    db.flush()
    clip = Clip(job_id=job.id, index=2, start=start, end=end, title="Why he missed",
                file_path=f"jobs/{job.id}/clip_2/final_video.mp4")
    db.add(clip)
    db.commit()
    job_dir = Path(settings.JOBS_DIR) / str(job.id)
    made.append(job_dir)
    clip_dir = job_dir / "clip_2"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "final_video.mp4").write_bytes(b"x" * 1000)
    return clip, clip_dir


def test_words_come_back_in_render_time_with_their_index(db):
    clip, clip_dir = _clip_with_render(db)
    (clip_dir / "words.json").write_text(json.dumps([
        {"word": "Why", "start": 0.0, "end": 0.4},
        {"word": "he", "start": 0.5, "end": 0.7},
        {"word": "", "start": 0.8, "end": 0.9},
        {"word": "missed", "start": 1.0, "end": 1.6},
    ]), encoding="utf-8")
    body = asyncio.run(features.clip_words(clip.id, db=db[0]))
    assert [w["word"] for w in body["words"]] == ["Why", "he", "missed"]
    assert [w["i"] for w in body["words"]] == [0, 1, 2]
    assert body["words"][2]["start"] == 1.0 and body["trim"] is None


def test_words_fall_back_to_the_job_words_shifted_to_the_clip(db):
    clip, _ = _clip_with_render(db, start=100.0, end=110.0)
    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    (job_dir / "words.json").write_text(json.dumps([
        {"word": "before", "start": 98.0, "end": 98.5},
        {"word": "inside", "start": 101.0, "end": 101.5},
    ]), encoding="utf-8")
    body = asyncio.run(features.clip_words(clip.id, db=db[0]))
    assert [w["word"] for w in body["words"]] == ["inside"]
    assert body["words"][0]["start"] == pytest.approx(1.0)


def test_reset_drops_the_trim_and_points_back_at_the_full_render(db):
    clip, clip_dir = _clip_with_render(db)
    clip.trim_start, clip.trim_end = 2.0, 5.0
    clip.file_path = f"jobs/{clip.job_id}/clip_2/final_video.trimmed.mp4"
    db[0].commit()
    body = asyncio.run(features.reset_trim(clip.id, db=db[0]))
    db[0].refresh(clip)
    assert body["trim"] is None and clip.trim_start is None and clip.trim_end is None
    assert clip.file_path.endswith("final_video.mp4") and "trimmed" not in clip.file_path


def test_the_payload_carries_the_render_options(db):
    clip, _ = _clip_with_render(db)
    assert clip_payload(clip)["render_options"] == {}
    clip.render_options = {"karaoke": False, "motion_title": True, "thumbnail_template": 2}
    db[0].commit()
    assert clip_payload(clip)["render_options"]["thumbnail_template"] == 2
