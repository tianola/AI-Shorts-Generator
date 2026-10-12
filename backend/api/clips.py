"""Clip records: listing, reading, manual trim/caption edits, upload status, re-render.

main.py mounts this router under /api. /clips/pending-upload is declared before
/clips/{clip_id} because FastAPI matches in registration order.
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import Session

from api.common import clip_payload
from config import settings
from database.models import Clip, Job
from database.session import get_database

logger = logging.getLogger(__name__)

router = APIRouter()


# YouTube ids are exactly 11 characters of [A-Za-z0-9_-]. Callers used to post
# placeholders such as "queued" here, which marked a clip as published and made
# the worker duplicate-guard skip the real upload forever.
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class ClipUploadStatus(BaseModel):
    uploaded: bool
    youtube_video_id: Optional[str] = None
    upload_error: Optional[str] = None

    @field_validator("youtube_video_id")
    @classmethod
    def normalize_video_id(cls, v):
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not YOUTUBE_VIDEO_ID_RE.match(v):
            raise ValueError("youtube_video_id must be an 11-character YouTube video id")
        return v

    @model_validator(mode="after")
    def published_needs_an_id(self):
        if self.uploaded and not self.youtube_video_id:
            raise ValueError("uploaded=true requires the real youtube_video_id")
        return self


class AIEditRequest(BaseModel):
    prompt: str


class RerenderClipRequest(BaseModel):
    caption_style: Optional[str] = "mrbeast"
    layout_mode: Optional[str] = "single_speaker"
    title: Optional[str] = None
    karaoke: Optional[bool] = None
    motion_title: Optional[bool] = None
    thumbnail_template: Optional[int] = None


class ClipCaptionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    hashtags: Optional[List[str]] = None


@router.post("/clips/{clip_id}/rerender")
async def rerender_clip(
    clip_id: int,
    request: RerenderClipRequest,
    db: Session = Depends(get_database)
):
    """Re-render a clip with updated subtitle preset, layout mode, and title overlay."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    # Load job to get default settings
    job = db.query(Job).filter(Job.id == clip.job_id).first()

    if request.caption_style:
        clip.caption_style = request.caption_style
    if request.layout_mode:
        clip.layout_mode = request.layout_mode
    if request.title:
        clip.title = request.title

    db.commit()
    db.refresh(clip)

    # Resolve karaoke/motion_title: explicit request > job default > True
    karaoke_enabled = request.karaoke if request.karaoke is not None else (job.default_karaoke if job else True)
    motion_title_enabled = request.motion_title if request.motion_title is not None else (job.default_motion_title if job else True)

    # Trigger re-render process
    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    clip_dir = job_dir / f"clip_{clip.index}"
    clip_dir.mkdir(parents=True, exist_ok=True)

    source_video = os.path.join(str(job_dir), "source.mp4")
    words_file = os.path.join(str(job_dir), "words.json")

    if not os.path.exists(source_video):
        raise HTTPException(status_code=400, detail="Source video not found for re-rendering")

    try:
        from worker import cut_clip
        from services.smart_crop import smart_crop_video
        from services.word_cropper import crop_words
        from services.captions import create_captions
        from services.render import burn_captions
        from services.motion_title import add_motion_title
        from services.sfx import add_automatic_sfx

        # 1. Cut clip
        short_path = os.path.join(str(clip_dir), "short.mp4")
        cut_clip(source_video, clip.start, clip.end, short_path)

        # 2. Vertical layout
        vertical_path = os.path.join(str(clip_dir), "vertical.mp4")
        smart_crop_video(short_path, vertical_path, mode=clip.layout_mode)

        # 3. Crop words & generate captions
        clip_words_file = os.path.join(str(clip_dir), "words.json")
        if os.path.exists(words_file):
            crop_words(words_file, clip.start, clip.end, clip_words_file)

        captions_path = os.path.join(str(clip_dir), "captions.ass")
        create_captions(clip_words_file, captions_path, style_name=clip.caption_style, karaoke=karaoke_enabled)

        # 4. Burn captions
        final_short = os.path.join(str(clip_dir), "final_short.mp4")
        burn_captions(vertical_path, captions_path, final_short)

        # 5. Add motion title (conditionally)
        if motion_title_enabled:
            title_path = os.path.join(str(clip_dir), "final_with_title.mp4")
            try:
                add_motion_title(final_short, clip.title or "Viral Moment", title_path)
                rendered_path = title_path
            except Exception:
                rendered_path = final_short
        else:
            rendered_path = final_short

        # 6. Add SFX with 100% voice preservation
        final_video_path = os.path.join(str(clip_dir), "final_video.mp4")
        add_automatic_sfx(rendered_path, final_video_path, title=clip.title or "", transcript="")

        # 7. Re-generate thumbnail with chosen template style
        from services.thumbnail import generate_thumbnail
        thumbnail_path = os.path.join(str(clip_dir), "thumbnail.jpg")
        tpl_idx = request.thumbnail_template if request.thumbnail_template is not None else (clip.index or 0)
        try:
            generate_thumbnail(final_video_path, thumbnail_path, clip.title or "Viral Moment", clip_index=tpl_idx, job_id=clip.job_id or 0)
            clip.thumbnail_path = os.path.relpath(thumbnail_path, settings.STORAGE_ROOT)
        except Exception as _te:
            logger.warning(f"Could not re-generate thumbnail for clip {clip.id}: {_te}")

        # Clean scratch files — aggressively remove all intermediate artifacts, keeping only final_video.mp4
        keep_files = {"final_video.mp4", "thumbnail.jpg", "metadata.json", "storyboard.jpg", "words.json"}
        for entry in os.listdir(str(clip_dir)):
            if entry not in keep_files and (entry.endswith(".mp4") or entry.endswith(".ass") or entry.endswith(".wav")):
                fp = os.path.join(str(clip_dir), entry)
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass

        clip.file_path = os.path.relpath(final_video_path, settings.STORAGE_ROOT)
        db.commit()
        db.refresh(clip)

        return {
            "message": "Clip successfully re-rendered",
            "clip": {
                "id": clip.id,
                "title": clip.title,
                "caption_style": clip.caption_style,
                "layout_mode": clip.layout_mode,
                "file_path": clip.file_path
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-render failed: {str(e)}")


@router.post("/clips/{clip_id}/ai-edit")
async def ai_edit_clip(
    clip_id: int,
    request: AIEditRequest,
    db: Session = Depends(get_database)
):
    """OmniRoute AI Director endpoint for natural language clip re-editing."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    from services.metadata import generate_metadata

    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    words_file = job_dir / "words.json"

    transcript = ""
    if words_file.exists():
        try:
            with open(words_file, "r", encoding="utf-8") as f:
                words_data = json.load(f)
            if isinstance(words_data, dict) and "words" in words_data:
                words_data = words_data["words"]
            clip_words = [w.get("word", "") for w in words_data if w.get("start", 0) >= clip.start and w.get("end", 0) <= clip.end]
            transcript = " ".join(clip_words)
        except Exception:
            pass

    if not transcript:
        transcript = clip.title or "Viral clip moment"

    prompt_instruction = f"User Instruction: {request.prompt}. Refine the clip metadata and hook accordingly."
    metadata = await generate_metadata(
        transcript=transcript + f"\n\nSpecial Instruction: {prompt_instruction}",
        title=clip.title,
        hook=clip.hook or ""
    )

    clip.title = metadata["title"]
    clip.description = metadata["description"]
    clip.hashtags = metadata["hashtags"]
    db.commit()
    db.refresh(clip)

    return {
        "message": f"OmniRoute AI Director successfully applied: '{request.prompt}'",
        "clip": {
            "id": clip.id,
            "title": clip.title,
            "description": clip.description,
            "hashtags": clip.hashtags,
            "file_path": clip.file_path
        }
    }


@router.get("/clips")
async def list_clips(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    job_id: Optional[int] = Query(None, description="Filter by job"),
    db: Session = Depends(get_database)
):
    """List clips with pagination."""
    query = db.query(Clip)
    if job_id is not None:
        query = query.filter(Clip.job_id == job_id)
    clips = query.order_by(Clip.id.desc()).offset(offset).limit(limit).all()

    return {
        "clips": [clip_payload(c) for c in clips],
        "total": len(clips)
    }


@router.get("/clips/pending-upload")
async def get_pending_uploads(
    max_retries: Optional[int] = Query(None, description="Max retries"),
    db: Session = Depends(get_database)
):
    """Get clips that need to be uploaded.

    Declared before /clips/{clip_id}: FastAPI matches in registration order,
    so as a later route "pending-upload" was captured by {clip_id} and died on
    int parsing (live 422).
    """
    if max_retries is None:
        max_retries = settings.MAX_UPLOAD_RETRIES

    clips = db.query(Clip).join(Job).filter(
        Clip.uploaded == False,
        Clip.upload_retry_count < max_retries,
        Job.status == "completed"
    ).all()

    return [{
        "id": clip.id,
        "job_id": clip.job_id,
        "file_path": clip.file_path,
        "thumbnail_path": clip.thumbnail_path,
        "title": clip.title,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "upload_retry_count": clip.upload_retry_count,
        "upload_error": clip.upload_error
    } for clip in clips]


@router.get("/clips/{clip_id}")
async def get_clip(
    clip_id: int,
    db: Session = Depends(get_database)
):
    """Get a single clip's full record."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    return {"clip": clip_payload(clip)}


@router.patch("/clips/{clip_id}/update-bounds")
async def update_clip_bounds(
    clip_id: int,
    payload: dict,
    db: Session = Depends(get_database)
):
    """Update clip start/end times."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip {clip_id} not found"
        )

    try:
        new_start = float(payload.get("start", clip.start))
        new_end = float(payload.get("end", clip.end))
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid start/end values: {e}")

    if new_end <= new_start:
        raise HTTPException(status_code=422, detail="end must be greater than start")

    clip.start = new_start
    clip.end = new_end
    db.commit()
    db.refresh(clip)

    return {
        "id": clip.id,
        "start": clip.start,
        "end": clip.end,
        "title": clip.title,
        "duration": clip.end - clip.start
    }


@router.patch("/clips/{clip_id}/update-caption")
async def update_clip_caption(
    clip_id: int,
    payload: ClipCaptionUpdate,
    db: Session = Depends(get_database)
):
    """Update clip title/caption/description/hashtags."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip {clip_id} not found"
        )

    if payload.title is not None:
        clip.title = payload.title
    if payload.description is not None:
        clip.description = payload.description
    if payload.hashtags is not None:
        clip.hashtags = payload.hashtags
    db.commit()
    db.refresh(clip)

    return {
        "id": clip.id,
        "title": clip.title,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "start": clip.start,
        "end": clip.end
    }


@router.patch("/clips/{clip_id}/upload-status")
async def update_clip_upload_status(
    clip_id: int,
    status_data: ClipUploadStatus,
    db: Session = Depends(get_database)
):
    """Update upload status for a clip."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip {clip_id} not found"
        )

    clip.uploaded = status_data.uploaded
    # Keep an existing id when a caller only reports a failure (id=None); a
    # blanket assignment wiped the id of a clip that had genuinely published.
    if status_data.youtube_video_id is not None:
        clip.youtube_video_id = status_data.youtube_video_id
    clip.upload_error = status_data.upload_error

    if not status_data.uploaded:
        clip.upload_retry_count += 1
    else:
        clip.published_at = datetime.utcnow()

    db.commit()
    db.refresh(clip)

    return {
        "id": clip.id,
        "uploaded": clip.uploaded,
        "youtube_video_id": clip.youtube_video_id,
        "upload_error": clip.upload_error,
        "upload_retry_count": clip.upload_retry_count,
        "published_at": clip.published_at
    }

