"""
Feature router: Multilingual translation, Precision Trim, and Smart Blur.

This workstream's endpoints were kept in their own module rather than folded into
the main job/clip routes, and they are registered in main.py with the same /api
prefix as the rest.
"""
import os
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pathlib import Path

from database.models import Job, Clip
from database.session import get_database
from config import settings
from pydantic import BaseModel, Field

router = APIRouter()


# ===========================================================================
# Precision trim
# ===========================================================================

class TrimRequest(BaseModel):
    start: object = Field(..., description="New start time: seconds or 'MM:SS'/'HH:MM:SS' timestamp")
    end: object = Field(..., description="New end time: seconds or 'MM:SS'/'HH:MM:SS' timestamp")
    force_reencode: bool = Field(
        False,
        description="Force a frame-accurate re-encode even when cut points align to keyframes",
    )


class TrimResponse(BaseModel):
    id: int
    start: float
    end: float
    duration: float
    file_path: str
    method: str
    used_fallback: bool
    message: str


def _clip_media_path(clip: Clip) -> str:
    """Resolve the clip's rendered final_video.mp4 on disk (absolute path)."""
    if clip.file_path:
        candidate = os.path.join(settings.STORAGE_ROOT, clip.file_path)
        if os.path.exists(candidate):
            return candidate
    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    clip_dir = job_dir / f"clip_{clip.index}"
    candidate = os.path.join(str(clip_dir), "final_video.mp4")
    if os.path.exists(candidate):
        return candidate
    return ""


@router.post("/clips/{clip_id}/trim", response_model=TrimResponse)
async def trim_rendered_clip(
    clip_id: int,
    payload: TrimRequest,
    db: Session = Depends(get_database),
):
    """Precision-trim a rendered clip.

    Uses the frame-aware trim service (stream-copy on keyframe-aligned cuts,
    re-encode otherwise). The clip's stored [start, end) bounds are unchanged;
    only the rendered file is cut to the requested window. The trimmed file is
    persisted at clip_dir/trimmed.mp4 and a fresh final_video.mp4 replaces the
    old render so the UI serves the trimmed version.
    """
    from services.trim import trim_clip, TrimValidationError, _as_seconds

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    media = _clip_media_path(clip)
    if not media:
        raise HTTPException(
            status_code=400,
            detail="Rendered clip file not found; re-render the clip before trimming",
        )

    # Accept seconds or "MM:SS"/"HH:MM:SS" timestamps; raise a clear 422 for
    # malformed input via the same parser used by the trim service.
    try:
        start = _as_seconds(payload.start, "start")
        end = _as_seconds(payload.end, "end")
    except TrimValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if end <= start:
        raise HTTPException(status_code=422, detail="end must be greater than start")

    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    clip_dir = job_dir / f"clip_{clip.index}"
    clip_dir.mkdir(parents=True, exist_ok=True)
    trimmed_path = os.path.join(str(clip_dir), "trimmed.mp4")
    final_path = os.path.join(str(clip_dir), "final_video.mp4")

    try:
        result = trim_clip(
            media,
            start,
            end,
            trimmed_path,
            force_reencode=payload.force_reencode,
        )
    except TrimValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Promote the trimmed file to the canonical final_video.mp4 so existing
    # media URLs keep working.
    try:
        os.replace(trimmed_path, final_path)
    except OSError:
        # Cross-device fallback
        import shutil
        shutil.move(trimmed_path, final_path)

    clip.trim_start = start
    clip.trim_end = end
    db.commit()
    db.refresh(clip)

    rel_path = os.path.relpath(final_path, settings.STORAGE_ROOT)
    clip.file_path = rel_path
    db.commit()

    return TrimResponse(
        id=clip.id,
        start=start,
        end=end,
        duration=result["duration"],
        file_path=rel_path,
        method=result["method"],
        used_fallback=result["used_fallback"],
        message=f"Clip trimmed ({result['method']}) to {start:.3f}s - {end:.3f}s",
    )


@router.get("/clips/{clip_id}/probe")
async def probe_clip_media(
    clip_id: int,
    db: Session = Depends(get_database),
):
    """Probe a clip's rendered media and report its real duration/streams."""
    from services.trim import probe_media, TrimValidationError

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    media = _clip_media_path(clip)
    if not media:
        raise HTTPException(status_code=400, detail="Rendered clip file not found")
    try:
        info = probe_media(media)
        return {"id": clip.id, "media": media, **info}
    except TrimValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ===========================================================================
# Multilingual / translation
# ===========================================================================

@router.post("/jobs/{job_id}/translate")
async def translate_job_transcript(
    job_id: int,
    db: Session = Depends(get_database),
):
    """Translate a job's already-written transcript to English.

    Currently reports 501: the LLM translation helpers this calls
    (_llm_translate_segments and friends) were never implemented in
    services.whisper, so the endpoint has answered with an unhandled ImportError -
    a bare 500 - for its whole life. English captions come from the transcriber:
    create the job with llm_translate=true and the worker runs Whisper with
    task=translate.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job_dir = Path(settings.JOBS_DIR) / str(job_id)
    segments_file = job_dir / "segments.json"
    words_file = job_dir / "words.json"
    if not segments_file.exists():
        raise HTTPException(
            status_code=400,
            detail=f"segments.json for job {job_id} not found; transcribe first",
        )

    try:
        from services.whisper import (
            _llm_translate_segments,
            _translate_linear_words,
            _write_transcript_files,
        )
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"Translating an existing transcript is not implemented ({e}). "
                   "Create the job with llm_translate=true to get English captions.",
        )

    try:
        with open(segments_file, "r", encoding="utf-8") as f:
            segments = json.load(f)
        native_words = []
        if words_file.exists():
            with open(words_file, "r", encoding="utf-8") as f:
                native_words = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read transcript: {e}")

    translated_segments = await _llm_translate_segments(
        segments,
        target_language="English",
        source_language=job.source_language,
    )
    translated_words = _translate_linear_words(
        native_words,
        segments,
        translated_segments,
    )

    _write_transcript_files(
        str(job_dir),
        translated_segments,
        translated_words,
        task="translate",
        language="en",
        native_segments_list=segments,
        native_words_list=native_words,
    )

    return {
        "job_id": job_id,
        "segments_file": str(job_dir / "segments.json"),
        "words_file": str(job_dir / "words.json"),
        "segments": translated_segments,
        "word_count": len(translated_words),
    }


# ===========================================================================
# Content safety: smart blur
# ===========================================================================

@router.post("/clips/{clip_id}/smart-blur")
async def apply_smart_blur(
    clip_id: int,
    classes: Optional[str] = Query(
        None,
        description="Comma-separated COCO class ids to blur (default: wine glass / alcohol)",
    ),
    db: Session = Depends(get_database),
):
    """Re-render a clip with object-tracking smart blur.

    Detects unsafe objects (default {45}: wine glass) across the clip and
    blurs the tracked region over time. The output replaces final_video.mp4
    so the UI keeps serving the blurred render.
    """
    from services.focus_tracking import (
        get_smart_blur_filter_for_video,
        unsafe_class_ids,
    )

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    media = _clip_media_path(clip)
    if not media:
        raise HTTPException(status_code=400, detail="Rendered clip file not found")

    blur_classes = None
    if classes:
        try:
            blur_classes = frozenset(int(x) for x in classes.split(",") if x.strip())
        except ValueError:
            raise HTTPException(status_code=422, detail="classes must be comma-separated integers")

    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    clip_dir = job_dir / f"clip_{clip.index}"
    clip_dir.mkdir(parents=True, exist_ok=True)

    blur_frag = get_smart_blur_filter_for_video(media, classes=blur_classes)
    if not blur_frag:
        return {
            "id": clip.id,
            "message": "No unsafe objects detected; clip unchanged",
            "blurred": False,
            "file_path": clip.file_path,
        }

    from services.render import render_single_pass
    from services.vertical import build_vertical_filter

    final_path = os.path.join(str(clip_dir), "final_video.mp4")
    tmp_path = os.path.join(str(clip_dir), "final_video.blur.mp4")
    try:
        render_single_pass(
            media,
            0.0,
            _probe_duration(media),
            tmp_path,
            vertical_frag=build_vertical_filter(media, mode=clip.layout_mode or "static"),
            smart_blur_frag=blur_frag,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Smart blur render failed: {e}")

    os.replace(tmp_path, final_path)
    clip.file_path = os.path.relpath(final_path, settings.STORAGE_ROOT)
    db.commit()

    return {
        "id": clip.id,
        "message": "Smart blur applied",
        "blurred": True,
        "file_path": clip.file_path,
        "classes": sorted(blur_classes or unsafe_class_ids()),
    }


def _probe_duration(video_path: str) -> float:
    import subprocess
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except Exception:
        return 0.0