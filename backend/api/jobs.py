"""Job lifecycle: create (URL or upload), start/resume/restart, read, suggest, open folder.

main.py mounts this router under /api.
"""
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.common import clip_payload, resolve_upload_privacy
from config import settings
from database.models import Clip, Job, JobProgress
from database.session import get_database

logger = logging.getLogger(__name__)

# Platform detection captured at import time. Kept as module attributes so tests
# can patch them WITHOUT mutating sys.platform/os.name process-wide (which breaks
# pathlib flavor selection on Windows).
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

router = APIRouter()


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

