"""Results: read published performance back, and compare the hook arms.

main.py mounts this router under /api. Everything here is read-only towards the
platforms - it asks YouTube for statistics and writes four columns - and nothing
in it uploads, publishes or schedules anything.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.session import get_database

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/analytics/sync-views")
async def sync_views(
    job_id: Optional[int] = Query(None, description="Only refresh one job's clips"),
    db: Session = Depends(get_database),
):
    """Pull view/like/comment counts for the clips we hold a real video id for.

    Safe to re-run; each pass just overwrites the stats with what YouTube says
    now. Clips whose id is a placeholder ('queued' and friends) are counted and
    skipped, never sent to the API.
    """
    from services.youtube_stats import sync_clip_stats

    try:
        result = sync_clip_stats(db, job_id=job_id)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The Google API client is not installed on this server: {exc}"
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=409,
            detail="YouTube is not connected yet - authorise the channel before syncing stats"
        )
    except Exception as exc:
        logger.exception("YouTube stats sync failed")
        raise HTTPException(status_code=502, detail=f"YouTube stats sync failed: {exc}")
    return {"sync": result}


@router.get("/analytics/hook-styles")
async def hook_styles(
    job_id: Optional[int] = Query(None, description="Only summarise one job's clips"),
    db: Session = Depends(get_database),
):
    """Per hook arm: clips shipped, clips published, and what they earned.

    Runs on whatever is already in the database, so call /analytics/sync-views
    first for numbers that are not stale.
    """
    from services.hook_analytics import summarize_hook_styles

    return {"hook_styles": summarize_hook_styles(db, job_id=job_id)}
