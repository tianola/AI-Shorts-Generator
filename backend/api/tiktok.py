"""TikTok connect and upload endpoints.

main.py mounts this router under /api, so the URLs a caller sees did not change.
"""
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database.models import Clip, Job
from database.session import get_database

router = APIRouter()

# ===========================================================================
# TIKTOK DIRECT PUBLISHING & BATCH UPLOAD ENDPOINTS
# ===========================================================================

@router.get("/tiktok/status")
async def get_tiktok_status():
    """Get current TikTok authentication and channel connection status."""
    from services.tiktok_upload import get_tiktok_auth_status
    return get_tiktok_auth_status()


class TikTokConnectRequest(BaseModel):
    username: str
    session_id: str
    auth_type: Optional[str] = "session_id"


@router.post("/tiktok/connect")
async def connect_tiktok_endpoint(req: TikTokConnectRequest):
    """Save TikTok session/token credentials."""
    from services.tiktok_upload import save_tiktok_credentials
    try:
        res = save_tiktok_credentials(
            username=req.username,
            session_id=req.session_id,
            auth_type=req.auth_type or "session_id"
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect TikTok: {str(e)}")


@router.post("/tiktok/disconnect")
async def disconnect_tiktok_endpoint():
    """Disconnect and remove stored TikTok credentials."""
    from services.tiktok_upload import disconnect_tiktok_account
    success = disconnect_tiktok_account()
    return {"success": success, "authenticated": False}


_TIKTOK_PRIVACY = {"public": "PUBLIC_TO_EVERYONE",
                   "unlisted": "PUBLIC_TO_SELF",
                   "private": "PRIVATE"}


def _resolve_tiktok_privacy(value: Optional[str]) -> str:
    """Map a YouTube-style visibility to its TikTok equivalent."""
    normalized = (value or settings.AUTO_UPLOAD_PRIVACY).lower().strip()
    if normalized not in _TIKTOK_PRIVACY:
        raise HTTPException(status_code=400, detail=f"Invalid privacy_level '{value}'")
    return _TIKTOK_PRIVACY[normalized]


@router.post("/clips/{clip_id}/upload-tiktok")
async def upload_clip_to_tiktok_endpoint(
    clip_id: int,
    privacy_level: Optional[str] = None,
    db: Session = Depends(get_database)
):
    """Publish a single clip directly to TikTok."""
    # Kept local: the TikTok client is optional and must not break importing the API.
    from services.tiktok_upload import upload_clip_to_tiktok

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    job = db.query(Job).filter(Job.id == clip.job_id).first() if clip.job_id else None

    source_title = None
    if job:
        job_dir = Path(settings.STORAGE_ROOT) / "jobs" / str(job.id)
        meta_file = job_dir / "meta.json"
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as mf:
                    mdata = json.load(mf)
                    source_title = mdata.get("title")
            except Exception:
                pass

    # Resolve video path
    video_path = None
    if clip.file_path:
        cand = Path(settings.STORAGE_ROOT) / clip.file_path
        if cand.exists():
            video_path = str(cand.resolve())
        elif os.path.exists(clip.file_path):
            video_path = str(Path(clip.file_path).resolve())

    if not video_path and job:
        clip_dir = Path(settings.STORAGE_ROOT) / "jobs" / str(job.id) / f"clip_{clip.index}"
        for cand_name in ["final_video.mp4", "final_short.mp4", "short.mp4"]:
            cand = clip_dir / cand_name
            if cand.exists():
                video_path = str(cand.resolve())
                break

    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail=f"Rendered video file not found for clip #{clip.index}")

    title = clip.title or f"Viral Moment #{clip.index + 1}"
    description = clip.description or "Watch this viral moment!"
    hashtags = clip.hashtags or ["#TikTok", "#Viral", "#FYP"]

    try:
        res = upload_clip_to_tiktok(
            video_path=video_path,
            title=title,
            description=description,
            hashtags=hashtags,
            source_title=source_title,
            privacy_level=_resolve_tiktok_privacy(privacy_level)
        )
        if res.get("video_id") or res.get("tiktok_url"):
            clip.tiktok_video_id = res.get("video_id") or res.get("tiktok_url")
            db.commit()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TikTok upload failed: {str(e)}")


@router.post("/jobs/{job_id}/upload-all-tiktok")
async def upload_all_clips_to_tiktok_endpoint(
    job_id: int,
    privacy_level: Optional[str] = None,
    db: Session = Depends(get_database)
):
    """Batch upload all clips from a job to TikTok."""
    from services.tiktok_upload import upload_all_clips_to_tiktok_for_job
    resolved = _resolve_tiktok_privacy(privacy_level)
    try:
        res = upload_all_clips_to_tiktok_for_job(job_id, db, privacy_level=resolved)
        return res
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TikTok batch upload failed: {str(e)}")
