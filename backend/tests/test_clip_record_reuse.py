"""Re-recording a clip must rewrite the row it reuses, not just return its id.

A reclaimed job re-enters the save stage for clips it already recorded. Reusing
the existing row is right: a fresh `Clip()` has no `youtube_video_id`, so the
re-upload guards stop firing and a short already on YouTube publishes twice. The
bug was that the reuse branch wrote nothing back, so job 110 re-rendered all ten
shorts onto new AI-chosen windows while rows 468-477 still reported the old grid
`start`/`end`/`title` - a row describing a file it no longer points at.

These four tests hold both halves of the requirement at once: the description
follows the re-run, and the publish identity does not.
"""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from database.models import Clip, Job
from database.session import SessionLocal, init_database


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    # A test that dies mid-flush leaves the session rolled back; deleting on top
    # of that raises again and poisons every later test in the file.
    session.rollback()
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _values(start, end, title):
    """One clip's descriptive payload, the way the save stage builds it."""
    return {
        "start": start,
        "end": end,
        "title": title,
        "hook": "Nobody expects the",
        "hook_style": "curiosity",
        "score": 61.5,
        "hook_score": 0.4,
        "coherence_score": 0.5,
        "connection_score": 0.6,
        "trend_score": 0.7,
        "score_breakdown": {"llm": 0.8, "audio": 0.2},
        "file_path": "jobs/77/clip_0/final_video.mp4",
        "thumbnail_path": "jobs/77/clip_0/thumbnail.jpg",
        "description": "how it works",
        "hashtags": ["#Shorts", "#radio"],
        "platform_metadata": {"youtube": {"title": title}},
        "caption_style": "mrbeast",
        "layout_mode": "single_speaker",
    }


def _job(db):
    job = Job(id=77, youtube_url="https://youtu.be/x", status="processing",
              target_clips=1, auto_upload=0)
    db.add(job)
    db.commit()
    return job


def test_record_clip_inserts_when_the_row_is_missing(db):
    _job(db)

    clip = worker.record_clip(db, 77, 0, _values(194.5, 223.4, "A real window"))

    assert clip.id is not None
    assert (clip.start, clip.end, clip.title) == (194.5, 223.4, "A real window")
    assert clip.uploaded is False
    assert clip.upload_retry_count == 0


def test_re_recording_rewrites_the_descriptive_columns(db):
    """The bug: the second run picked new windows, and the row kept the old ones."""
    _job(db)
    first = worker.record_clip(db, 77, 0, _values(0.0, 30.0, "Grid window"))
    row_id = first.id
    db.commit()

    again = worker.record_clip(db, 77, 0, _values(194.5, 223.4, "AI window"))

    assert again.id == row_id, "re-recording must reuse the row, not add one"
    assert (again.start, again.end, again.title) == (194.5, 223.4, "AI window")
    assert again.score == 61.5
    assert again.score_breakdown == {"llm": 0.8, "audio": 0.2}
    assert db.query(Clip).filter(Clip.job_id == 77, Clip.index == 0).count() == 1


def test_reuse_leaves_the_publish_identity_alone(db):
    """The reason the row is reused at all, pinned so the fix cannot undo it.

    A row that already carries a `youtube_video_id` must keep it across a
    re-record: with the id gone, the guard at the upload step reads a fresh
    clip and publishes the same short a second time. The queued publish clock
    has to survive too, or a re-run silently cancels a scheduled release.
    """
    _job(db)
    due_at = datetime.utcnow() + timedelta(hours=3)
    published_at = datetime(2026, 11, 12, 7, 23, 45)
    clip = worker.record_clip(db, 77, 0, _values(0.0, 30.0, "old"))
    clip.youtube_video_id = "ICgUjmEUlbg"
    clip.uploaded = True
    clip.upload_retry_count = 2
    clip.publish_due_at = due_at
    clip.published_at = published_at
    db.commit()

    again = worker.record_clip(db, 77, 0, _values(194.5, 223.4, "new"))

    assert again.youtube_video_id == "ICgUjmEUlbg"
    assert again.uploaded is True
    assert again.upload_retry_count == 2
    assert again.publish_due_at == due_at
    assert again.published_at == published_at
    assert again.title == "new", "the description still has to follow the run"



def test_record_clip_never_invents_columns_that_do_not_exist(db):
    """A stray key must not silently widen what a re-record overwrites."""
    _job(db)
    values = _values(0.0, 30.0, "t")
    values["youtube_video_id"] = "INJECTED"

    with pytest.raises(ValueError, match="youtube_video_id"):
        worker.record_clip(db, 77, 0, values)
