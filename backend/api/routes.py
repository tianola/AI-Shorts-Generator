"""
API routes for AI Shorts Generator.
FastAPI endpoints for job and clip management.
"""
import asyncio
import redis.asyncio as redis
import os
import json
import shutil
import time
from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Query
from typing import List, Optional
from datetime import datetime
from sqlalchemy.orm import Session

from database.models import Job, Clip, JobProgress
from database.session import get_database
from config import settings
from pydantic import BaseModel

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
    try:
        r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)))
        pubsub = r.pubsub()
        await pubsub.subscribe(f"job_progress_{job_id}")
        async for message in pubsub.listen():
            if message['type'] == 'message':
                data = message['data'].decode('utf-8')
                await websocket.send_text(data)
    except Exception:
        pass
    finally:
        try:
            await pubsub.unsubscribe(f"job_progress_{job_id}")
            await r.close()
        except Exception:
            pass

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

class ClipUploadStatus(BaseModel):
    uploaded: bool
    youtube_video_id: Optional[str] = None
    upload_error: Optional[str] = None

class ClipResponse(BaseModel):
    id: int
    job_id: int
    index: int
    start: float
    end: float
    title: Optional[str]
    hook: Optional[str]
    score: Optional[float]
    hook_score: Optional[float] = None
    coherence_score: Optional[float] = None
    connection_score: Optional[float] = None
    trend_score: Optional[float] = None
    file_path: Optional[str]
    thumbnail_path: Optional[str]
    description: Optional[str]
    hashtags: Optional[List[str]]
    uploaded: bool
    youtube_video_id: Optional[str]
    upload_error: Optional[str]
    upload_retry_count: int
    published_at: Optional[datetime]
    created_at: datetime

class AIEditRequest(BaseModel):
    prompt: str

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

    from pathlib import Path
    from config import settings
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

    if clip.file_path:
        try:
            full_video_path = os.path.join(settings.STORAGE_ROOT, clip.file_path)
            clip_dir = os.path.dirname(full_video_path)
            if os.path.exists(clip_dir):
                vertical_path = os.path.join(clip_dir, "vertical.mp4")
                if not os.path.exists(vertical_path):
                    vertical_path = os.path.join(clip_dir, "short.mp4")

                if os.path.exists(vertical_path):
                    from services.render import add_title_overlay
                    from services.sfx import add_automatic_sfx
                    new_final = os.path.join(clip_dir, f"final_ai_{int(time.time())}.mp4")
                    title_path = os.path.join(clip_dir, "final_with_title.mp4")
                    add_title_overlay(vertical_path, clip.title, title_path)
                    add_automatic_sfx(title_path, new_final, title=clip.title, transcript=transcript)

                    clip.file_path = os.path.relpath(new_final, settings.STORAGE_ROOT)
                    db.commit()
                    db.refresh(clip)
        except Exception as e:
            print(f"AI edit re-render error: {e}")

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

@router.patch("/clips/{clip_id}/update-bounds")
async def update_clip_bounds(
    clip_id: int,
    start: float,
    end: float,
    db: Session = Depends(get_database)
):
    """Update clip start/end times for iterative refinement."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip {clip_id} not found"
        )

    clip.start = start
    clip.end = end
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
    title: str,
    db: Session = Depends(get_database)
):
    """Update clip title/caption for iterative refinement."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip {clip_id} not found"
        )

    clip.title = title
    db.commit()
    db.refresh(clip)

    return {
        "id": clip.id,
        "title": clip.title,
        "start": clip.start,
        "end": clip.end
    }

@router.get("/clips/pending-upload")
async def get_pending_uploads(
    max_retries: int = Query(None, description="Max retries (query param)"),
    db: Session = Depends(get_database)
):
    """
    Get clips that need to be uploaded (failed uploads with retries remaining).

    Args:
        max_retries: Maximum retry count (defaults to MAX_UPLOAD_RETRIES)

    Returns:
        List of clips pending upload
    """
    if max_retries is None:
        from config import settings
        max_retries = settings.MAX_UPLOAD_RETRIES

    # Get clips that:
    # 1. Are not uploaded
    # 2. Have retry count < max_retries
    # 3. Belong to a completed job
    clips = db.query(Clip).join(Job).filter(
        Clip.uploaded == False,
        Clip.upload_retry_count < max_retries,
        Job.status == "completed"
    ).all()

    result = [{
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

    return result


@router.get("/jobs/{job_id}/suggest-clips")
async def suggest_clips(job_id: int):
    """
    Suggest top clips for a job using the clip selector AI (useful to preview or regenerate selections).
    Returns the same structure as select_best_clips would choose.
    """
    import os
    from services.clip_selector import select_best_clips
    from config import settings

    job_segments = os.path.join(settings.JOBS_DIR, str(job_id), "segments.json")
    if not os.path.exists(job_segments):
        raise HTTPException(status_code=404, detail=f"segments.json for job {job_id} not found")

    clips = await select_best_clips(job_segments, settings.NUM_CLIPS)
    return {"clips": clips}


@router.post("/jobs")
async def create_job(
    youtube_url: str,
    style_prompt: Optional[str] = None,
    whisper_model: Optional[str] = "tiny",
    db: Session = Depends(get_database)
):
    """
    Create a new video processing job.

    Args:
        youtube_url: YouTube URL to process
        style_prompt: Optional style/b-roll visual prompt modifier
        whisper_model: The whisper model to use for transcription

    Returns:
        Job creation response with job_id
    """
    job = Job(
        youtube_url=youtube_url,
        style_prompt=style_prompt,
        whisper_model=whisper_model,
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

@router.post("/jobs/upload")
async def create_job_from_upload(
    file: UploadFile = File(...),
    style_prompt: Optional[str] = Form(None),
    whisper_model: Optional[str] = Form("tiny"),
    db: Session = Depends(get_database)
):
    """
    Create a new job from a locally uploaded video file (MP4/WebM/MOV).

    The file is saved as ``source.mp4`` inside the job directory so the
    worker pipeline picks it up without needing to download anything.
    """
    allowed_ext = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{ext or 'unknown'}'. Allowed: mp4, webm, mov, m4v, mkv"
        )

    job = Job(
        youtube_url=f"upload://{file.filename or 'local.mp4'}",
        style_prompt=style_prompt,
        whisper_model=whisper_model,
        status="created"
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    job_dir = os.path.join(settings.STORAGE_ROOT, "jobs", str(job.id))
    os.makedirs(job_dir, exist_ok=True)
    dest_path = os.path.join(job_dir, "source.mp4")

    try:
        with open(dest_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        db.delete(job)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {str(e)}"
        )
    finally:
        await file.close()

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
    """
    Get job details and all associated clips.

    Args:
        job_id: The job ID

    Returns:
        Job details with clips
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found"
        )

    # Get all clips for this job
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
        clips=[{
            "id": clip.id,
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
            "file_path": clip.file_path,
            "thumbnail_path": clip.thumbnail_path,
            "description": clip.description,
            "hashtags": clip.hashtags,
            "uploaded": clip.uploaded,
            "youtube_video_id": clip.youtube_video_id,
            "upload_error": clip.upload_error,
            "upload_retry_count": clip.upload_retry_count,
            "published_at": clip.published_at,
            "created_at": clip.created_at
        } for clip in clips]
    )

@router.get("/jobs")
async def list_jobs(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_database)
):
    """
    List all jobs with pagination.

    Args:
        limit: Maximum number of jobs to return
        offset: Number of jobs to skip

    Returns:
        List of jobs
    """
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

# ====================
# Clip Endpoints
# ====================

@router.patch("/clips/{clip_id}/upload-status")
async def update_clip_upload_status(
    clip_id: int,
    status_data: ClipUploadStatus,
    db: Session = Depends(get_database)
):
    """
    Update upload status for a clip.
    Called by n8n after YouTube upload attempt.

    Args:
        clip_id: The clip ID
        status_data: Upload status data

    Returns:
        Updated clip data
    """
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip {clip_id} not found"
        )

    # Update fields
    clip.uploaded = status_data.uploaded
    clip.youtube_video_id = status_data.youtube_video_id
    clip.upload_error = status_data.upload_error

    # Increment retry count if not uploaded
    if not status_data.uploaded and status_data.upload_error:
        clip.upload_retry_count += 1

    # Set published_at if uploaded
    if status_data.uploaded and clip.published_at is None:
        clip.published_at = datetime.utcnow()

    db.commit()
    db.refresh(clip)

    return {
        "id": clip.id,
        "uploaded": clip.uploaded,
        "youtube_video_id": clip.youtube_video_id,
        "upload_error": clip.upload_error,
        "upload_retry_count": clip.upload_retry_count
    }

@router.websocket("/ws/progress/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: int):
    await manager.connect(websocket, job_id)
    task = asyncio.create_task(redis_listener(websocket, job_id))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)
        task.cancel()

@router.get("/health")
async def health():
    """Liveness check endpoint."""
    return {"status": "healthy"}

@router.post("/jobs/{job_id}/open-folder")
async def open_job_folder(job_id: int):
    """Open the job storage folder in the local OS file explorer."""
    job_dir = os.path.abspath(os.path.join(settings.STORAGE_ROOT, "jobs", str(job_id)))
    if not os.path.exists(job_dir):
        raise HTTPException(status_code=404, detail=f"Job folder not found: {job_dir}")
    
    try:
        if os.name == 'nt':
            os.startfile(job_dir)
        elif os.name == 'posix':
            import subprocess
            subprocess.run(['xdg-open', job_dir])
        return {"success": True, "path": job_dir}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {str(e)}")

@router.get("/system/info")
async def get_system_info():
    """Get hardware info and available whisper models."""
    import psutil
    import torch
    
    # Hardware info
    cpu_cores = psutil.cpu_count(logical=False)
    cpu_threads = psutil.cpu_count(logical=True)
    ram_gb = round(psutil.virtual_memory().total / (1024**3), 1)
    
    has_gpu = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if has_gpu else "None (CPU only)"
    vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1) if has_gpu else 0
    
    # Check AMD GPU on Windows (WMI)
    if not has_gpu and os.name == 'nt':
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True
            )
            if result.stdout.strip():
                gpu_name = result.stdout.strip().split('\n')[0]
                
            ram_result = subprocess.run(
                ["powershell", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty AdapterRAM"],
                capture_output=True, text=True
            )
            if ram_result.stdout.strip():
                ram_val = int(ram_result.stdout.strip().split('\n')[0])
                vram_gb = round(abs(ram_val) / (1024**3), 1)
        except Exception:
            pass

    # Available models
    models = [
        {"id": "groq", "name": "Groq Whisper Turbo (Cloud, Fastest)", "speed_multiplier": 0.1, "ram_required": 0.0},
        {"id": "tiny", "name": "Tiny (Local, Fast, low accuracy)", "speed_multiplier": 1.0, "ram_required": 1.0},
        {"id": "base", "name": "Base (Local, Fast, low accuracy)", "speed_multiplier": 1.5, "ram_required": 1.5},
        {"id": "small", "name": "Small (Local, Balanced)", "speed_multiplier": 3.0, "ram_required": 2.5},
        {"id": "medium", "name": "Medium (Local, Slow, high accuracy)", "speed_multiplier": 6.0, "ram_required": 5.0},
        {"id": "large-v3", "name": "Large V3 (Local, Very slow, best accuracy)", "speed_multiplier": 10.0, "ram_required": 10.0},
    ]
    
    # Check E:\01-AI-PROJECTS\whisper-models for .pt files
    local_models_dir = r"E:\01-AI-PROJECTS\whisper-models"
    local_pt_models = []
    if os.path.exists(local_models_dir):
        for f in os.listdir(local_models_dir):
            if f.endswith(".pt"):
                model_id = f.replace(".pt", "")
                local_pt_models.append({
                    "id": f"local_pt_{model_id}",
                    "name": f"{model_id}.pt (Local PyTorch)",
                    "speed_multiplier": 15.0 if "large" in model_id else 8.0,
                    "ram_required": 10.0 if "large" in model_id else 5.0,
                    "path": os.path.join(local_models_dir, f)
                })
    
    all_models = models + local_pt_models
    
    # Determine recommended
    recommended = "groq" if os.getenv("GROQ_API_KEY") else "tiny"
    if not os.getenv("GROQ_API_KEY"):
        if cpu_threads >= 16 and ram_gb >= 16:
            recommended = "small"
        if has_gpu and vram_gb >= 8:
            recommended = "medium"
        if has_gpu and vram_gb >= 12:
            recommended = "large-v3"
        
    for m in all_models:
        m["is_recommended"] = (m["id"] == recommended)
        # Estimate time for a 10 min video (600s)
        base_time = 30 if has_gpu else 90
        est_seconds = base_time * m["speed_multiplier"]
        if m["id"] == "groq":
            m["est_time"] = "~10 sec"
        elif est_seconds < 60:
            m["est_time"] = f"~{int(est_seconds)} sec"
        else:
            m["est_time"] = f"~{int(est_seconds/60)} min"
            
    return {
        "hardware": {
            "cpu": f"{cpu_cores} Cores / {cpu_threads} Threads",
            "ram": f"{ram_gb} GB",
            "gpu": gpu_name,
            "vram": f"{vram_gb} GB" if vram_gb else "N/A"
        },
        "models": all_models
    }