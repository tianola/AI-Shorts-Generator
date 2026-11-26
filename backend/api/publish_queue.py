"""The publish queue as a list: every clip waiting for its YouTube release, in upload order.

A clip waits here from the moment it gets a release slot until it has a YouTube video id.
The worker reads the same rows, so this list is the schedule the channel will follow.

The stored times are what the queue first asked for. A backlog makes them stale: a clip due
yesterday still waits for the clips ahead of it and then goes out one upload gap after the last
one, and YouTube gets a release time moved to just after the upload (publish_queue moves a
release that has passed). So each row also carries the expected upload and release times, worked
out the same way the worker will: in due order, one upload per UPLOAD_MIN_GAP_SECONDS after the
last publish, never before the row's own due time.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from config import settings
from database.models import Clip
from database.session import get_database

router = APIRouter()


def _utc(moment) -> str | None:
    return f"{moment.isoformat()}Z" if moment else None


def expected_times(db, rows, now: datetime) -> list:
    """(upload, release) per row, in the order given. The upload jitter is left out."""
    gap = timedelta(seconds=max(0, int(settings.UPLOAD_MIN_GAP_SECONDS)))
    lead = timedelta(seconds=max(60, int(settings.PUBLISH_SCHEDULE_LEAD_SECONDS)))
    last = db.query(func.max(Clip.published_at)).filter(
        Clip.youtube_video_id.isnot(None), Clip.youtube_video_id != "",
    ).scalar()
    slot = max(now, last + gap) if last else now
    out = []
    for clip in rows:
        due = clip.publish_due_at or slot
        upload = max(slot, due)
        release = clip.scheduled_publish_at
        if release is None or release < upload + lead:
            release = upload + lead
        out.append((upload, release))
        slot = upload + gap
    return out


@router.get("/publish/queue")
def publish_queue(db: Session = Depends(get_database), now: datetime | None = None):
    rows = db.query(Clip).filter(
        Clip.scheduled_publish_at.isnot(None),
        or_(Clip.youtube_video_id.is_(None), Clip.youtube_video_id == ""),
        Clip.uploaded == False,  # noqa: E712
    ).all()
    # The worker uploads the row with the earliest publish_due_at first; list them that way.
    far = datetime.max
    rows.sort(key=lambda c: (c.publish_due_at or far, c.scheduled_publish_at or far, c.id))
    expected = expected_times(db, rows, now or datetime.utcnow())
    return {
        "gap_seconds": settings.PUBLISH_SCHEDULE_GAP_SECONDS,
        "upload_gap_seconds": settings.UPLOAD_MIN_GAP_SECONDS,
        "items": [
            {
                "id": clip.id,
                "job_id": clip.job_id,
                "index": clip.index,
                "title": clip.title or f"Clip {clip.index}",
                "release_at": _utc(clip.scheduled_publish_at),
                "upload_at": _utc(clip.publish_due_at),
                "expected_upload_at": _utc(upload),
                "expected_release_at": _utc(release),
            }
            for clip, (upload, release) in zip(rows, expected)
        ],
    }
