"""The publish queue as a list: every clip waiting for its YouTube release, in release order.

A clip waits here from the moment it gets a release slot until it has a YouTube video id.
The worker reads the same rows, so this list is the schedule the channel will follow.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from config import settings
from database.models import Clip
from database.session import get_database

router = APIRouter()


def _utc(moment) -> str | None:
    return f"{moment.isoformat()}Z" if moment else None


@router.get("/publish/queue")
def publish_queue(db: Session = Depends(get_database)):
    rows = db.query(Clip).filter(
        Clip.scheduled_publish_at.isnot(None),
        or_(Clip.youtube_video_id.is_(None), Clip.youtube_video_id == ""),
        Clip.uploaded == False,  # noqa: E712
    ).order_by(Clip.scheduled_publish_at.asc(), Clip.id.asc()).all()
    return {
        "gap_seconds": settings.PUBLISH_SCHEDULE_GAP_SECONDS,
        "items": [
            {
                "id": clip.id,
                "job_id": clip.job_id,
                "index": clip.index,
                "title": clip.title or f"Clip {clip.index}",
                "release_at": _utc(clip.scheduled_publish_at),
                "upload_at": _utc(clip.publish_due_at),
            }
            for clip in rows
        ],
    }
