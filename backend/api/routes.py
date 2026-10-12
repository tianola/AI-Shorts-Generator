"""
API routes for AI Shorts Generator.
FastAPI endpoints for job and clip management, OpusClip preset selection, and 1-click re-rendering.
"""
import asyncio
import redis.asyncio as redis
import os
import json
import re
import logging
import shutil
import subprocess
import sys
from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Query
from typing import List, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from pathlib import Path

from database.models import Job, Clip, JobProgress
from database.session import SessionLocal, get_database
from config import settings
from pydantic import BaseModel, field_validator, model_validator
from api.common import resolve_upload_privacy

logger = logging.getLogger(__name__)


def clip_payload(clip: Clip) -> dict:
    """Single wire shape for a clip, used by every endpoint that returns one.

    Three hand-copied variants used to drift (platform_metadata, tiktok_video_id
    and the score breakdown were missing from some of them), so the frontend
    received different fields depending on which endpoint it called.
    """
    return {
        "id": clip.id,
        "job_id": clip.job_id,
        "index": clip.index,
        "start": clip.start,
        "end": clip.end,
        "title": clip.title,
        "hook": clip.hook,
        "score": clip.score,
        "hook_score": clip.hook_score,
        "coherence_score": clip.coherence_score,
        "connection_score": clip.connection_score,
        "trend_score": clip.trend_score,
        "score_breakdown": clip.score_breakdown,
        "caption_style": clip.caption_style or "mrbeast",
        "layout_mode": clip.layout_mode or "single_speaker",
        "virality_explanation": clip.virality_explanation,
        "trim_start": clip.trim_start,
        "trim_end": clip.trim_end,
        "translated_transcript": clip.translated_transcript,
        "file_path": clip.file_path,
        "thumbnail_path": clip.thumbnail_path,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "platform_metadata": clip.platform_metadata,
        "uploaded": clip.uploaded,
        "youtube_video_id": clip.youtube_video_id,
        "tiktok_video_id": clip.tiktok_video_id,
        "upload_error": clip.upload_error,
        "upload_retry_count": clip.upload_retry_count,
        "published_at": clip.published_at,
        "created_at": clip.created_at,
    }


# Platform detection captured at import time. Kept as module attributes so tests
# can patch them WITHOUT mutating sys.platform/os.name process-wide (which breaks
# pathlib flavor selection on Windows).
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# WebSocket manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, job_id: int):
        await websocket.accept()
        if job_id not in self.active_connections:
            self.active_connections[job_id] = []
        self.active_connections[job_id].append(websocket)

    def disconnect(self, websocket: WebSocket, job_id: int):
        self.active_connections[job_id].remove(websocket)
        if not self.active_connections[job_id]:
            del self.active_connections[job_id]

manager = ConnectionManager()

async def redis_listener(websocket: WebSocket, job_id: int):
    # 1. Try Redis pub/sub if Redis is available
    try:
        r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), socket_connect_timeout=1)
        pubsub = r.pubsub()
        await pubsub.subscribe(f"job_progress_{job_id}")
        async for message in pubsub.listen():
            if message['type'] == 'message':
                data = message['data'].decode('utf-8')
                await websocket.send_text(data)
        return
    except Exception:
        pass

    # 2. Resilient DB Poller fallback (runs smoothly on Windows local without Redis)
    last_stage = None
    last_percent = None
    last_status = None
    while True:
        try:
            db: Session = SessionLocal()
            try:
                job = db.query(Job).filter(Job.id == job_id).first()
                progress = db.query(JobProgress).filter(JobProgress.job_id == job_id).first()
                if not job:
                    break
                stage = progress.current_stage if progress else "Initializing..."
                percent = progress.progress_percent if progress else 0
                status = job.status
                clips_count = len(job.clips) if job.clips else 0

                if stage != last_stage or percent != last_percent or status != last_status:
                    last_stage = stage
                    last_percent = percent
                    last_status = status
                    payload = json.dumps({
                        "job_id": job_id,
                        "stage": stage,
                        "percent": percent,
                        "status": status,
                        "clips_count": clips_count,
                    })
                    await websocket.send_text(payload)

                if status in ("completed", "failed"):
                    break
            finally:
                db.close()
            await asyncio.sleep(0.35)
        except Exception:
            break

router = APIRouter()

# ====================
# Pydantic Models
# ====================

class JobCreateResponse(BaseModel):
    job_id: int
    status: str
    url: str

class JobResponse(BaseModel):
    id: int
    youtube_url: str
    status: str
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    clips: List[dict] = []
    current_stage: Optional[str] = None
    progress_percent: Optional[int] = None

class JobListResponse(BaseModel):
    jobs: List[JobResponse]

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


@router.get("/system/presets")
async def get_system_presets():
    """Return available OpusClip subtitle style presets and multi-layout modes."""
    from services.captions import PRESETS
    presets_list = [
        {
            "id": k,
            "name": v.name,
            "highlight_color": v.highlight_color,
            "primary_color": v.primary_color,
            "jump": v.jump,
            "add_emojis": v.add_emojis
        }
        for k, v in PRESETS.items()
    ]
    layouts_list = [
        {"id": "single_speaker", "name": "Solo Speaker Follow", "description": "Smooth YOLO face tracking"},
        {"id": "split_screen", "name": "Podcast Split (9:16)", "description": "Stacked 2-speaker view"},
        {"id": "fit_blur", "name": "Blurred Background Fit", "description": "Full frame with blurred backdrop"}
    ]
    return {"presets": presets_list, "layouts": layouts_list}


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


@router.post("/jobs/{job_id}/start")
async def start_job(job_id: int, db: Session = Depends(get_database)):
    """Manually start a created job so the worker can pick it up."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.status in ("processing", "created"):
        return {"id": job.id, "status": job.status, "message": "Job is already running or queued."}
    job.status = "created"
    job.error_message = None
    db.commit()
    db.refresh(job)
    return {"id": job.id, "status": job.status, "message": "Job started."}


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: int, db: Session = Depends(get_database)):
    """Resume a failed or completed job by resetting it to created."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.status not in ("failed", "completed"):
        return {"id": job.id, "status": job.status, "message": "Job can only be resumed from failed or completed."}
    job.status = "created"
    job.error_message = None
    db.commit()
    db.refresh(job)
    return {"id": job.id, "status": job.status, "message": "Job resumed."}


@router.post("/jobs/{job_id}/restart")
async def restart_job(job_id: int, db: Session = Depends(get_database)):
    """Restart a job from scratch: delete all artifacts and reset status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job_dir = Path(settings.JOBS_DIR) / str(job_id)

    # An upload:// job has no URL to re-fetch, so wiping source.mp4 made
    # restart permanently fatal. Carry the source across the cleanup.
    preserved_source = None
    if job_dir.exists():
        source_file = job_dir / "source.mp4"
        if source_file.exists() and (job.youtube_url or "").startswith("upload://"):
            staging = job_dir.parent / f"job_{job_id}_source.keep"
            source_file.replace(staging)
            preserved_source = staging

    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)

    if preserved_source is not None and preserved_source.exists():
        preserved_source.replace(job_dir / "source.mp4")

    # Reset status
    job.status = "created"
    job.error_message = None
    db.commit()
    db.refresh(job)
    return {"id": job.id, "status": job.status, "message": "Job restarted from scratch."}


@router.get("/jobs/{job_id}/suggest-clips")
async def suggest_clips_for_job(
    job_id: int,
    db: Session = Depends(get_database)
):
    """Re-run AI clip suggestion from segments."""
    from services.clip_selector import select_clips
    import json
    job_dir = os.path.join(settings.JOBS_DIR, str(job_id))
    job_segments = os.path.join(job_dir, "segments.json")
    if not os.path.exists(job_segments):
        raise HTTPException(status_code=404, detail=f"segments.json for job {job_id} not found")

    meta_file = os.path.join(job_dir, "meta.json")
    source_title = None
    if os.path.exists(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as mf:
                source_title = json.load(mf).get("title")
        except Exception:
            pass

    clips = await select_clips(job_segments, settings.NUM_CLIPS, video_title=source_title)
    return {"clips": clips}



@router.post("/jobs")
async def create_job(
    youtube_url: str,
    style_prompt: Optional[str] = None,
    whisper_model: Optional[str] = "tiny",
    caption_style: Optional[str] = "mrbeast",
    layout_mode: Optional[str] = "auto",
    target_clips: Optional[int] = 10,
    auto_upload: Optional[bool] = False,
    llm_translate: Optional[bool] = False,
    upload_privacy: Optional[str] = None,
    db: Session = Depends(get_database)
):
    """Create a new video processing job."""
    if not youtube_url.strip():
        raise HTTPException(status_code=400, detail="youtube_url must not be empty")

    job = Job(
        youtube_url=youtube_url,
        style_prompt=style_prompt,
        whisper_model=whisper_model,
        default_caption_style=caption_style,
        default_layout_mode=layout_mode,
        target_clips=target_clips or 10,
        auto_upload=bool(auto_upload),
        llm_translate=bool(llm_translate),
        upload_privacy=resolve_upload_privacy(upload_privacy),
        status="created"
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    return JobCreateResponse(
        job_id=job.id,
        status=job.status,
        url=job.youtube_url
    )


@router.post("/test/zero-view-diagnostic")
async def zero_view_diagnostic_endpoint(
    youtube_url: Optional[str] = Form(None),
    style_prompt: Optional[str] = Form(None),
    whisper_model: Optional[str] = Form("tiny"),
    caption_style: Optional[str] = Form("mrbeast"),
    layout_mode: Optional[str] = Form("auto"),
    target_clips: Optional[int] = Form(10),
    auto_upload: Optional[bool] = Form(False),
    llm_translate: Optional[bool] = Form(False),
    upload_privacy: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_database)
):
    """Create a diagnostic job whose clips each use a different hook style.
    Takes either a youtube_url or a multipart file upload.
    """
    if not youtube_url and not file:
        raise HTTPException(status_code=400, detail="Provide either youtube_url or a 'file' upload")

    num_clips = target_clips or 10

    # Create job
    job = Job(
        youtube_url=youtube_url or f"upload://{file.filename if file else 'unknown'}",
        style_prompt=style_prompt,
        whisper_model=whisper_model,
        default_caption_style=caption_style,
        default_layout_mode=layout_mode,
        target_clips=num_clips,
        auto_upload=bool(auto_upload),
        llm_translate=bool(llm_translate),
        upload_privacy=resolve_upload_privacy(upload_privacy),
        status="created"
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    if file:
        job_dir = Path(settings.JOBS_DIR) / str(job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        dest_path = job_dir / "source.mp4"
        with open(dest_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    # Set up for zero-view diagnostic: varied hook styles
    base_styles = ["aggressive", "curiosity", "story-driven"]
    repeat_times = (num_clips + len(base_styles) - 1) // len(base_styles)
    job.hook_style_list = (base_styles * repeat_times)[:num_clips]
    job.is_zero_view_diagnostic = True
    db.commit()
    db.refresh(job)

    return {
        "job_id": job.id,
        "message": f"Zero-view diagnostic job created with {num_clips} clips using varied hook styles."
    }


@router.post("/jobs/upload")
async def create_job_from_upload(
    file: UploadFile = File(...),
    style_prompt: Optional[str] = Form(None),
    whisper_model: Optional[str] = Form("tiny"),
    caption_style: Optional[str] = Form("mrbeast"),
    layout_mode: Optional[str] = Form("auto"),
    target_clips: Optional[int] = Form(10),
    auto_upload: Optional[bool] = Form(False),
    llm_translate: Optional[bool] = Form(False),
    upload_privacy: Optional[str] = Form(None),
    db: Session = Depends(get_database)
):
    """Create a new job from a locally uploaded video file."""
    allowed_ext = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Allowed: {', '.join(sorted(allowed_ext))}"
        )

    job = Job(
        youtube_url=f"upload://{file.filename}",
        style_prompt=style_prompt,
        whisper_model=whisper_model,
        default_caption_style=caption_style,
        default_layout_mode=layout_mode,
        target_clips=target_clips or 10,
        auto_upload=bool(auto_upload),
        llm_translate=bool(llm_translate),
        upload_privacy=resolve_upload_privacy(upload_privacy),
        status="created"
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    job_dir = Path(settings.JOBS_DIR) / str(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)
    dest_path = job_dir / "source.mp4"

    try:
        with open(dest_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        job.status = "failed"
        job.error_message = f"Failed to save uploaded file: {str(e)}"
        db.commit()
        raise HTTPException(status_code=500, detail=job.error_message)

    return JobCreateResponse(
        job_id=job.id,
        status=job.status,
        url=job.youtube_url
    )


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: int,
    db: Session = Depends(get_database)
):
    """Get job details and all associated clips."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    clips = db.query(Clip).filter(Clip.job_id == job_id).all()
    progress = db.query(JobProgress).filter(JobProgress.job_id == job_id).first()

    return JobResponse(
        id=job.id,
        youtube_url=job.youtube_url,
        status=job.status,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        current_stage=progress.current_stage if progress else None,
        progress_percent=progress.progress_percent if progress else None,
        clips=[clip_payload(clip) for clip in clips]
    )


@router.get("/jobs")
async def list_jobs(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_database)
):
    """List all jobs with pagination."""
    jobs = db.query(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit).all()

    return JobListResponse(
        jobs=[{
            "id": job.id,
            "youtube_url": job.youtube_url,
            "status": job.status,
            "error_message": job.error_message,
            "created_at": job.created_at,
            "updated_at": job.updated_at
        } for job in jobs]
    )


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


@router.websocket("/ws/progress/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: int):
    """WebSocket endpoint for real-time job progress updates."""
    await manager.connect(websocket, job_id)
    listener_task = asyncio.create_task(redis_listener(websocket, job_id))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)
        listener_task.cancel()


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@router.post("/jobs/{job_id}/open-folder")
async def open_job_folder(job_id: int):
    """Open the job's storage directory in the native OS file explorer.

    On Windows this launches Explorer at the job directory. The path is
    resolved from the *host-visible* storage root: when the API runs inside a
    container (Docker), the container's view of the path is meaningless to the
    Windows host, so we refuse with a clear message instead of silently doing
    nothing (the old behaviour hid this failure).
    """
    job_dir = Path(settings.JOBS_DIR) / str(job_id)
    resolved = job_dir.resolve()

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Job directory not found: {resolved}")

    # The API cannot open a native Explorer window for a path that only exists
    # inside a Linux container. Fail loudly with guidance instead of a silent no-op.
    if os.path.exists("/.dockerenv"):
        raise HTTPException(
            status_code=503,
            detail=(
                "Cannot open Windows Explorer from inside the Docker container. "
                "Run the native backend (start_ai_shorts.ps1 / python main.py) so the "
                "Open Folder button can open the host folder, or open the folder manually at: "
                f"{resolved}"
            ),
        )

    try:
        if IS_WINDOWS:
            # Use explorer.exe with a normalized native Windows path. subprocess
            # avoids os.startfile's swallowed exceptions and gives us a return code.
            # explorer.exe returns immediately; a non-zero exit means the path or
            # invocation was rejected.
            win_path = resolved.as_posix() if resolved.drive == "" else str(resolved)
            result = subprocess.run(
                ["explorer.exe", win_path], check=False, capture_output=True, timeout=15
            )
            if result.returncode not in (0, 1):
                # Explorer can return 1 for benign reasons; treat only non-0/1 as failure.
                logger.warning("explorer.exe returned rc=%s for %s", result.returncode, win_path)
            return {"status": "ok", "path": str(resolved), "opened_with": "explorer.exe"}
        elif IS_MACOS:
            subprocess.Popen(["open", str(resolved)])
            return {"status": "ok", "path": str(resolved), "opened_with": "open"}
        else:
            # Linux desktop (non-Docker): try xdg-open; if unavailable, tell the user
            # where the folder is rather than pretending success.
            if subprocess.run(
                ["which", "xdg-open"], capture_output=True
            ).returncode == 0:
                subprocess.Popen(["xdg-open", str(resolved)])
                return {"status": "ok", "path": str(resolved), "opened_with": "xdg-open"}
            logger.info("xdg-open not available; returning path for manual open: %s", resolved)
            return {"status": "ok", "path": str(resolved), "opened_with": None}
    except Exception as e:
        logger.exception("Failed to open folder %s", resolved)
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {str(e)}")


@router.get("/video/metadata")
async def get_video_metadata(url: str):
    """Quickly extract video metadata (title, duration, thumbnail) without downloading."""
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    try:
        import yt_dlp
        ydl_opts = {
            "extract_flat": True,
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "socket_timeout": 15,
        }
        if getattr(settings, "YT_COOKIES_FILE", None) and os.path.exists(settings.YT_COOKIES_FILE):
            ydl_opts["cookiefile"] = settings.YT_COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False)
            dur = float(info.get("duration") or 0)
            mins = int(dur // 60)
            secs = int(dur % 60)
            dur_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            return {
                "title": info.get("title") or "Unknown Video",
                "duration": dur,
                "duration_formatted": dur_str,
                "thumbnail": info.get("thumbnail"),
            }
    except Exception as e:
        logger.warning(f"Failed to fetch metadata for {url}: {e}")
        return {
            "title": None,
            "duration": 0,
            "duration_formatted": None,
            "thumbnail": None,
        }


@router.get("/system/info")
async def get_system_info(duration: Optional[float] = None):
    """Get accurate hardware specs and dynamically estimated Whisper models based on video duration."""
    try:
        import psutil
    except ImportError:
        psutil = None
    try:
        import torch
    except ImportError:
        torch = None

    import platform
    import subprocess

    cpu_name = None
    try:
        res = subprocess.run(
            ["wmic", "cpu", "get", "name"],
            capture_output=True, text=True, timeout=5
        )
        lines = [
            line.strip() for line in res.stdout.strip().split("\n")
            if line.strip() and line.strip().lower() != "name"
        ]
        if lines:
            cpu_name = lines[0]
    except Exception:
        pass

    if not cpu_name:
        cpu_name = platform.processor() or "Multi-core CPU"

    threads = psutil.cpu_count(logical=True) if psutil else (os.cpu_count() or 8)
    ram_gb = round(psutil.virtual_memory().total / (1024**3), 1) if psutil else 16.0

    cpu_label = f"{cpu_name} ({threads} Threads)"
    ram_label = f"{ram_gb} GB"

    has_gpu = False
    gpu_name = "None (CPU Multi-threaded)"
    vram_gb = 0.0
    if torch is not None and torch.cuda.is_available():
        has_gpu = True
        gpu_name = torch.cuda.get_device_name(0)
        vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1)

    # Dynamic speed scaling factors based on real hardware performance
    is_high_end_cpu = threads >= 16 and ram_gb >= 16
    cpu_factor = 0.7 if is_high_end_cpu else (1.0 if threads >= 8 else 1.8)

    models_def = [
        {
            "id": "groq",
            "name": "Groq Whisper Turbo (Cloud ⚡ Instant)",
            "cloud": True,
            "rtf_gpu": 0.01,
            "rtf_cpu": 0.01,
            "vram_req": 0.0,
            "ram_req": 0.0,
        },
        {
            "id": "tiny",
            "name": "Tiny (Local, Ultra-Fast)",
            "cloud": False,
            "rtf_gpu": 0.015,
            "rtf_cpu": 0.05 * cpu_factor,
            "vram_req": 0.6,
            "ram_req": 0.8,
        },
        {
            "id": "base",
            "name": "Base (Local, Fast)",
            "cloud": False,
            "rtf_gpu": 0.025,
            "rtf_cpu": 0.10 * cpu_factor,
            "vram_req": 1.0,
            "ram_req": 1.2,
        },
        {
            "id": "small",
            "name": "Small (Local, Balanced)",
            "cloud": False,
            "rtf_gpu": 0.050,
            "rtf_cpu": 0.22 * cpu_factor,
            "vram_req": 2.0,
            "ram_req": 2.5,
        },
        {
            "id": "medium",
            "name": "Medium (Local, High Accuracy)",
            "cloud": False,
            "rtf_gpu": 0.120,
            "rtf_cpu": 0.55 * cpu_factor,
            "vram_req": 4.5,
            "ram_req": 5.0,
        },
        {
            "id": "large-v3",
            "name": "Large V3 (Local, Best Quality)",
            "cloud": False,
            "rtf_gpu": 0.220,
            "rtf_cpu": 1.10 * cpu_factor,
            "vram_req": 8.0,
            "ram_req": 9.0,
        },
    ]

    # Compute accurate duration-based estimates
    effective_duration = float(duration) if duration and float(duration) > 0 else 300.0  # default 5 min reference
    groq_key = os.getenv("GROQ_API_KEY")

    if groq_key:
        recommended = "groq"
    elif has_gpu and vram_gb >= 8:
        recommended = "medium"
    elif has_gpu and vram_gb >= 4:
        recommended = "small"
    elif is_high_end_cpu:
        recommended = "small"
    elif threads >= 8:
        recommended = "base"
    else:
        recommended = "tiny"

    formatted_models = []
    for m in models_def:
        is_rec = (m["id"] == recommended)
        rtf = m["rtf_gpu"] if has_gpu else m["rtf_cpu"]
        if m["cloud"]:
            est = "~5-10 sec"
        else:
            sec = max(5, int(effective_duration * rtf))
            if sec < 60:
                est = f"~{sec} sec"
            else:
                mins = round(sec / 60, 1)
                est = f"~{int(mins) if mins.is_integer() else mins} min"

        formatted_models.append({
            "id": m["id"],
            "name": m["name"],
            "est_time": est,
            "rtf": rtf,
            "is_cloud": m["cloud"],
            "is_recommended": is_rec,
            "ram_required": m["vram_req"] if has_gpu else m["ram_req"],
        })

    return {
        "hardware": {
            "cpu": cpu_label,
            "ram": ram_label,
            "gpu": gpu_name,
            "vram": f"{vram_gb} GB" if vram_gb > 0 else "N/A",
        },
        "models": formatted_models,
    }
