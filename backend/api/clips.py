"""Clip records: listing, reading, manual trim/caption edits, upload status, re-render.

main.py mounts this router under /api. /clips/pending-upload is declared before
/clips/{clip_id} because FastAPI matches in registration order.
"""
import json
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

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


def probe_media_duration(video_path: str) -> Optional[float]:
    """Real duration of a media file, or None when it cannot be known.

    None means "no evidence", never "zero": a failed probe must not become a
    ceiling that rejects every window the operator asks for.
    """
    try:
        from services.trim import probe_media
        duration = float(probe_media(video_path)["duration"])
    except Exception as exc:  # no ffprobe, unreadable file, no video stream
        logger.info("Could not probe %s: %s", video_path, exc)
        return None
    return duration if duration > 0 else None


def source_duration(job_id: Optional[int]) -> Optional[float]:
    """Length of a job's downloaded source, which is what clip windows index.

    ffprobe on a multi-hundred-megabyte mp4 only reads the container header, but
    it is still a blocking subprocess, so callers on the event loop must reach
    this through ``run_in_threadpool``.
    """
    if job_id is None:
        return None
    source = Path(settings.JOBS_DIR) / str(job_id) / "source.mp4"
    if not source.exists():
        return None
    return probe_media_duration(str(source))


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


def _rerender_media(clip_dir, source_video: str, words_file: str, *, window,
                   layout_mode: str, caption_style: str, title: Optional[str],
                   karaoke_enabled: bool, motion_title_enabled: bool,
                   thumbnail_template: Optional[int], job_id: Optional[int],
                   clip_index: int, trim) -> dict:
    """The blocking half of a re-render, run in a worker thread.

    ``cut_clip`` through ``generate_thumbnail`` is minutes of ffmpeg and PIL work
    per clip; it must never run on the event loop, and it must not hold a session
    either, so everything here takes plain values and returns plain values and
    the endpoint does the recording.

    ``trim`` is the row's recorded ``(trim_start, trim_end)`` or ``None``: the
    render is rebuilt from ``window`` alone, so without this the operator's
    manual trim would be silently undone by their own re-render.
    """
    from worker import cut_clip
    from services.smart_crop import smart_crop_video
    from services.word_cropper import crop_words
    from services.captions import create_captions
    from services.render import burn_captions
    from services.motion_title import add_motion_title
    from services.sfx import add_automatic_sfx
    from services.thumbnail import generate_thumbnail
    from services.trim import TrimValidationError
    from utils import clip_trim, derived_cache

    # This endpoint rebuilds with inputs the worker's cache key does not know
    # about (karaoke, thumbnail template), so any manifest left behind would
    # vouch for artifacts it did not build. Drop the claims first; the next
    # worker run then rebuilds on its own terms instead of trusting a key that
    # no longer describes this output. The trimmed artifact is in that list
    # because the render it was cut from is about to be replaced.
    for _stale in ("short.mp4", "vertical.mp4", "words.json", "captions.ass",
                   "final_short.mp4", "final_with_title.mp4", "final_with_sfx.mp4",
                   "final_video.mp4", clip_trim.TRIMMED_ARTIFACT_NAME):
        derived_cache.forget(os.path.join(str(clip_dir), _stale))

    # 1. Cut clip
    short_path = os.path.join(str(clip_dir), "short.mp4")
    cut_clip(source_video, window[0], window[1], short_path)

    # 2. Vertical layout
    vertical_path = os.path.join(str(clip_dir), "vertical.mp4")
    smart_crop_video(short_path, vertical_path, mode=layout_mode)

    # 3. Crop words & generate captions
    clip_words_file = os.path.join(str(clip_dir), "words.json")
    if os.path.exists(words_file):
        crop_words(words_file, window[0], window[1], clip_words_file)

    captions_path = os.path.join(str(clip_dir), "captions.ass")
    create_captions(clip_words_file, captions_path, style_name=caption_style,
                    karaoke=karaoke_enabled)

    # 4. Burn captions
    final_short = os.path.join(str(clip_dir), "final_short.mp4")
    burn_captions(vertical_path, captions_path, final_short)

    # 5. Add motion title (conditionally)
    if motion_title_enabled:
        title_path = os.path.join(str(clip_dir), "final_with_title.mp4")
        try:
            add_motion_title(final_short, title or "Viral Moment", title_path)
            rendered_path = title_path
        except Exception:
            rendered_path = final_short
    else:
        rendered_path = final_short

    # 6. Add SFX with 100% voice preservation
    final_video_path = os.path.join(str(clip_dir), "final_video.mp4")
    add_automatic_sfx(rendered_path, final_video_path, title=title or "", transcript="")

    # 7. Re-generate thumbnail with chosen template style
    thumbnail_relative = None
    thumbnail_path = os.path.join(str(clip_dir), "thumbnail.jpg")
    tpl_idx = thumbnail_template if thumbnail_template is not None else (clip_index or 0)
    try:
        generate_thumbnail(final_video_path, thumbnail_path, title or "Viral Moment",
                           clip_index=tpl_idx, job_id=job_id or 0)
        thumbnail_relative = os.path.relpath(thumbnail_path, settings.STORAGE_ROOT)
    except Exception as _te:
        logger.warning("Could not re-generate thumbnail for job %s clip %s: %s",
                       job_id, clip_index, _te)

    # Clean scratch files - aggressively remove all intermediate artifacts,
    # keeping only what the clip actually serves. The trimmed cut is not in the
    # keep list on purpose: it is rebuilt from the new render just below, so a
    # trim of the previous render cannot survive this pass looking current.
    keep_files = {"final_video.mp4", "thumbnail.jpg", "metadata.json",
                  "storyboard.jpg", "words.json"}
    for entry in os.listdir(str(clip_dir)):
        if entry not in keep_files and (entry.endswith(".mp4") or entry.endswith(".ass")
                                        or entry.endswith(".wav")):
            fp = os.path.join(str(clip_dir), entry)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass

    served_path = final_video_path
    trim_reapplied = False
    trim_error = None
    if trim is not None and trim[0] is not None and trim[1] is not None:
        try:
            trimmed = clip_trim.sync_stored_trim(clip_dir, window, trim[0], trim[1])
            if trimmed:
                served_path = trimmed
                trim_reapplied = True
        except TrimValidationError as e:
            # The new render is not the media the window was chosen against
            # (shorter, or re-cropped). Serving the untrimmed render is the
            # honest fallback, but it has to be said out loud: the alternative
            # is a clip that publishes whole while the Studio shows it trimmed.
            trim_error = str(e)
            logger.warning("Clip %s of job %s keeps an untrimmed re-render: its "
                           "recorded trim %s-%ss no longer fits (%s)", clip_index,
                           job_id, trim[0], trim[1], e)

    return {"served_path": served_path, "final_video_path": final_video_path,
            "thumbnail_relative": thumbnail_relative,
            "trim_reapplied": trim_reapplied, "trim_error": trim_error}


@router.post("/clips/{clip_id}/rerender")
async def rerender_clip(
    clip_id: int,
    request: RerenderClipRequest,
    db: Session = Depends(get_database)
):
    """Re-render a clip with updated subtitle preset, layout mode, and title overlay.

    The render itself is a worker-thread job (see ``_rerender_media``); this
    function only reads the row, dispatches, and records what came back. The
    clip's recorded manual trim is re-applied to the fresh render, so a
    re-render cannot publish something the operator already rejected.
    """
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

    # Resolve karaoke/motion_title: explicit request > job default > True
    karaoke_enabled = request.karaoke if request.karaoke is not None else (job.default_karaoke if job else True)
    motion_title_enabled = request.motion_title if request.motion_title is not None else (job.default_motion_title if job else True)
    # Stored, so reopening the studio shows the switches the render was made with.
    clip.render_options = {
        "karaoke": bool(karaoke_enabled),
        "motion_title": bool(motion_title_enabled),
        "thumbnail_template": request.thumbnail_template,
    }

    db.commit()
    db.refresh(clip)

    # Trigger re-render process
    job_dir = Path(settings.JOBS_DIR) / str(clip.job_id)
    clip_dir = job_dir / f"clip_{clip.index}"
    clip_dir.mkdir(parents=True, exist_ok=True)

    source_video = os.path.join(str(job_dir), "source.mp4")
    words_file = os.path.join(str(job_dir), "words.json")

    if not os.path.exists(source_video):
        raise HTTPException(status_code=400, detail="Source video not found for re-rendering")

    # Snapshot everything the pipeline needs as plain values: the thread that
    # runs it must not reach through the ORM or the session.
    plan = {
        "clip_dir": clip_dir,
        "source_video": source_video,
        "words_file": words_file,
        "window": (clip.start, clip.end),
        "layout_mode": clip.layout_mode,
        "caption_style": clip.caption_style,
        "title": clip.title,
        "karaoke_enabled": karaoke_enabled,
        "motion_title_enabled": motion_title_enabled,
        "thumbnail_template": request.thumbnail_template,
        "job_id": clip.job_id,
        "clip_index": clip.index,
        "trim": (clip.trim_start, clip.trim_end),
    }

    try:
        result = await run_in_threadpool(_rerender_media, **plan)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-render failed: {str(e)}")

    if result["thumbnail_relative"]:
        clip.thumbnail_path = result["thumbnail_relative"]
    clip.file_path = os.path.relpath(result["served_path"], settings.STORAGE_ROOT)
    db.commit()
    db.refresh(clip)

    if result["trim_error"]:
        message = ("Clip successfully re-rendered, but its recorded trim could not "
                   f"be re-applied to the new render: {result['trim_error']}")
    elif result["trim_reapplied"]:
        message = "Clip successfully re-rendered and re-trimmed"
    else:
        message = "Clip successfully re-rendered"

    return {
        "message": message,
        # The whole record, so the studio can replace what it holds (the title it shows
        # is then the one that was burned into the render).
        "clip": clip_payload(clip),
        "trim_reapplied": result["trim_reapplied"],
        "trim_error": result["trim_error"],
    }


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


@router.post("/clips/{clip_id}/retitle")
async def retitle_clip(clip_id: int, db: Session = Depends(get_database)):
    """Give a clip a new title from what it says, then re-render so the burned title changes.

    The title follows the same brief and checker as new clips (services/title_style.py): it
    must be true to the clip's words. When no candidate passes, the clip keeps its title.
    """
    import json as _json
    from services.title_style import suggest_title

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    words_file = Path(settings.JOBS_DIR) / str(clip.job_id) / f"clip_{clip.index}" / "words.json"
    if not words_file.exists():
        raise HTTPException(status_code=400, detail="This clip has no transcript to title from")
    words = _json.loads(words_file.read_text(encoding="utf-8"))
    transcript = " ".join(str(w.get("word") or "") for w in words)

    new_title = await suggest_title(transcript)
    if not new_title:
        raise HTTPException(status_code=422, detail="No title passed the checks for this clip; kept the old one")

    job = db.query(Job).filter(Job.id == clip.job_id).first()
    request = RerenderClipRequest(
        caption_style=clip.caption_style,
        layout_mode=clip.layout_mode,
        title=new_title,
        karaoke=(clip.render_options or {}).get("karaoke", job.default_karaoke if job else True),
        motion_title=(clip.render_options or {}).get("motion_title", job.default_motion_title if job else True),
        thumbnail_template=(clip.render_options or {}).get("thumbnail_template"),
    )
    return await rerender_clip(clip_id, request, db)


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
    """Update clip start/end times: the window the next render will cut.

    ``start``/``end`` are source-absolute seconds and feed straight into the
    worker's ``cut_clip`` call and its derived-cache window key, so a value that
    is not a real window does not fail here — it fails an ffmpeg run later, or
    worse keys a cache entry on NaN. ``float()`` accepts "nan"/"inf" and every
    comparison against NaN is False, which is why the old ``end <= start`` guard
    let them through; the finite checks below are what close that.
    """
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

    for name, value in (("start", new_start), ("end", new_end)):
        if not math.isfinite(value):
            raise HTTPException(
                status_code=422,
                detail=f"{name} must be a finite number of seconds, got {payload.get(name)!r}")

    if new_start < 0:
        raise HTTPException(status_code=422, detail="start must be >= 0")

    if new_end <= new_start:
        raise HTTPException(status_code=422, detail="end must be greater than start")

    duration = await run_in_threadpool(source_duration, clip.job_id)
    if duration is not None and new_end > duration:
        raise HTTPException(
            status_code=422,
            detail=f"end {new_end:.3f}s is past the end of the source "
                   f"({duration:.3f}s); nothing could be cut there")

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


@router.patch("/clips/{clip_id}/upload-status")
async def update_clip_upload_status(
    clip_id: int,
    status_data: ClipUploadStatus,
    db: Session = Depends(get_database)
):
    """Record what a caller observed about one publish.

    The strike counter is NOT this endpoint's to spend. `worker._record_publish_failure`
    already charges the attempt when an upload fails - through the queue's own pass or
    through `publish_clip_for_operator`, which is what the Studio's publish buttons now
    call - and the Studio then reports the same outcome here. Charging twice retired a
    clip after ~1.5 real attempts, and retirement nulls `publish_due_at` and
    `scheduled_publish_at`, so the clip lost its release slot and left the queue without
    a word. One failure, one strike, spent in exactly one place.

    Success does still belong here, and it clears the queue's clocks the same way
    `_ship_clip_to_youtube` does: a row that reads `uploaded=1` with a due time still set
    is the clip-461 phantom, and every consumer of `publish_due_at` treats it as work.
    """
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

    # A failure report changes nothing else: no retry_count (see the docstring)
    # and no clock, because a caller's report is not the queue's decision to
    # retry, retire, or move the release.
    if status_data.uploaded:
        clip.published_at = datetime.utcnow()
        clip.publish_due_at = None
        clip.publish_attempt_at = None
        clip.upload_error = None

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

