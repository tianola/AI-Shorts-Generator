"""
Feature router: Multilingual translation, Precision Trim, and Smart Blur.

This workstream's endpoints were kept in their own module rather than folded into
the main job/clip routes, and they are registered in main.py with the same /api
prefix as the rest.
"""
import logging
import math
import os
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from pathlib import Path

from database.models import Job, Clip
from database.session import get_database
from config import settings
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

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
    """Resolve the clip's *pristine render* on disk (absolute path).

    ``final_video.mp4`` is what a trim window is measured against, so it is what
    this returns even after ``clip.file_path`` moved to the trimmed artifact. A
    row whose render lives somewhere else (an older layout, a hand-set path) still
    resolves through ``file_path``, as long as that is not the trimmed cut.
    """
    from utils import clip_trim

    clip_dir = Path(settings.JOBS_DIR) / str(clip.job_id) / f"clip_{clip.index}"
    candidate = clip_trim.base_render_path(clip_dir)
    if os.path.exists(candidate):
        return candidate
    if clip.file_path:
        stored = os.path.join(settings.STORAGE_ROOT, clip.file_path)
        if (os.path.exists(stored)
                and os.path.basename(stored) != clip_trim.TRIMMED_ARTIFACT_NAME):
            return stored
    return ""


def _clip_dir(clip: Clip) -> Path:
    return Path(settings.JOBS_DIR) / str(clip.job_id) / f"clip_{clip.index}"


def _validated_seconds(raw, name: str, parse) -> float:
    """Parse a trim time and refuse the values comparisons cannot see.

    ``float("nan")`` parses, and ``nan <= x`` is False for every x, so the
    ordering guard downstream waves it through; it then reaches an ffmpeg ``-ss``
    argument and the derived-cache key.
    """
    from services.trim import TrimValidationError

    try:
        value = parse(raw, name)
    except TrimValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not math.isfinite(value):
        raise HTTPException(status_code=422,
                            detail=f"{name} must be a finite number of seconds, "
                                   f"got {raw!r}")
    return value


@router.post("/clips/{clip_id}/trim", response_model=TrimResponse)
async def trim_rendered_clip(
    clip_id: int,
    payload: TrimRequest,
    db: Session = Depends(get_database),
):
    """Precision-trim a rendered clip into an artifact of its own.

    Uses the frame-aware trim service (stream-copy on keyframe-aligned cuts,
    re-encode otherwise) on the clip's pristine ``final_video.mp4`` and writes
    ``final_video.trimmed.mp4`` next to it - the render is never overwritten, so
    the trim can be re-applied, undone, or rebuilt, and a second trim cuts the
    render again instead of cutting an already-trimmed file.

    The artifact is registered in the directory's derived-artifact manifest keyed
    by the clip window, the trim window and the render it was cut from, which is
    what makes it safe to keep: a re-render that replaces the render invalidates
    the trim, and ``POST /clips/{id}/rerender`` then rebuilds it.

    ``clip.start``/``clip.end`` are untouched - they still describe the window the
    source was cut at, which is what every cache key in the pipeline reads. The
    trim is seconds *into that render*.
    """
    from services.trim import TrimValidationError, as_seconds
    from utils import clip_trim

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
    start = _validated_seconds(payload.start, "start", as_seconds)
    end = _validated_seconds(payload.end, "end", as_seconds)

    if end <= start:
        raise HTTPException(status_code=422, detail="end must be greater than start")

    clip_dir = _clip_dir(clip)
    clip_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = await run_in_threadpool(
            clip_trim.apply_trim, clip_dir, (clip.start, clip.end), start, end,
            force_reencode=payload.force_reencode)
    except TrimValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    rel_path = os.path.relpath(result["path"], settings.STORAGE_ROOT)
    clip.trim_start = start
    clip.trim_end = end
    clip.file_path = rel_path
    db.commit()
    db.refresh(clip)

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
    """Probe a clip's rendered media and report its real duration/streams.

    Reports the pristine render, because that is the timeline a trim window
    indexes; the trimmed artifact is named alongside it so the operator can see
    which file the row is actually serving.
    """
    from services.trim import probe_media, TrimValidationError
    from utils import clip_trim

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    media = _clip_media_path(clip)
    if not media:
        raise HTTPException(status_code=400, detail="Rendered clip file not found")
    try:
        info = await run_in_threadpool(probe_media, media)
    except TrimValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    trimmed = clip_trim.trimmed_path(_clip_dir(clip))
    return {
        "id": clip.id,
        "media": media,
        "trimmed_media": (os.path.relpath(trimmed, settings.STORAGE_ROOT)
                          if os.path.exists(trimmed) else None),
        "trim_window": ([clip.trim_start, clip.trim_end]
                        if clip.trim_start is not None and clip.trim_end is not None
                        else None),
        **info,
    }


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

def _smart_blur_render(clip_dir, media: str, blur_classes, layout_mode: Optional[str],
                       window, trim):
    """Detect, re-render, promote and re-trim: every blocking step of the blur.

    Object tracking is a YOLO pass over the whole clip and the re-render is a
    full ffmpeg encode; both belong in a worker thread. The values come in plain
    and go out plain so the event loop never holds a session across them.

    The output replaces ``final_video.mp4``, which is the render a recorded trim
    was cut from - so the trim is rebuilt from the blurred file here rather than
    left as a cut of a render that no longer exists.
    """
    from services.focus_tracking import get_smart_blur_filter_for_video, unsafe_class_ids
    from services.render import render_single_pass
    from services.vertical import build_vertical_filter
    from services.trim import TrimValidationError
    from utils import clip_trim, derived_cache

    blur_frag = get_smart_blur_filter_for_video(media, classes=blur_classes)
    if not blur_frag:
        return {"blurred": False, "served_path": None, "trim_reapplied": False,
                "trim_error": None, "classes": sorted(blur_classes or unsafe_class_ids())}

    final_path = clip_trim.base_render_path(clip_dir)
    tmp_path = os.path.join(str(clip_dir), "final_video.blur.mp4")
    try:
        render_single_pass(
            media,
            0.0,
            _probe_duration(media),
            tmp_path,
            vertical_frag=build_vertical_filter(media, mode=layout_mode or "static"),
            smart_blur_frag=blur_frag,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Smart blur render failed: {e}")

    os.replace(tmp_path, final_path)
    # The render this trim was cut from has been replaced: drop the claim before
    # deciding what the clip serves now.
    derived_cache.forget(clip_trim.trimmed_path(clip_dir))

    served_path = final_path
    trim_reapplied = False
    trim_error = None
    if trim is not None and trim[0] is not None and trim[1] is not None:
        try:
            retrimmed = clip_trim.sync_stored_trim(clip_dir, window, trim[0], trim[1])
            if retrimmed:
                served_path = retrimmed
                trim_reapplied = True
        except TrimValidationError as e:
            trim_error = str(e)
            logger.warning("Clip %s keeps an untrimmed blur: its recorded trim "
                           "%s-%ss no longer fits (%s)", clip_dir, trim[0], trim[1], e)

    return {"blurred": True, "served_path": served_path,
            "trim_reapplied": trim_reapplied, "trim_error": trim_error,
            "classes": sorted(blur_classes or unsafe_class_ids())}


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
    blurs the tracked region over time. The output replaces final_video.mp4, and
    a clip with a recorded manual trim has that trim re-applied to the blurred
    render, so the short the operator approved is still the short they get.
    """
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

    clip_dir = _clip_dir(clip)
    clip_dir.mkdir(parents=True, exist_ok=True)

    result = await run_in_threadpool(
        _smart_blur_render, clip_dir, media, blur_classes, clip.layout_mode,
        (clip.start, clip.end), (clip.trim_start, clip.trim_end))

    if not result["blurred"]:
        return {
            "id": clip.id,
            "message": "No unsafe objects detected; clip unchanged",
            "blurred": False,
            "trim_reapplied": False,
            "trim_error": None,
            "file_path": clip.file_path,
        }

    clip.file_path = os.path.relpath(result["served_path"], settings.STORAGE_ROOT)
    db.commit()
    db.refresh(clip)

    if result["trim_error"]:
        message = ("Smart blur applied, but the recorded trim could not be "
                   f"re-applied to the blurred render: {result['trim_error']}")
    elif result["trim_reapplied"]:
        message = "Smart blur applied and the recorded trim re-applied"
    else:
        message = "Smart blur applied"

    return {
        "id": clip.id,
        "message": message,
        "blurred": True,
        "trim_reapplied": result["trim_reapplied"],
        "trim_error": result["trim_error"],
        "file_path": clip.file_path,
        "classes": result["classes"],
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

def _clip_words(clip: Clip) -> list:
    """The clip's words in render time (0 = the render's first frame), in order.

    The rerender writes ``words.json`` next to the render already shifted to the render's
    own timeline, which is what the trim route measures in. A clip rendered before that
    file existed falls back to the job's words, shifted by the clip start.
    """
    own = _clip_dir(clip) / "words.json"
    if own.exists():
        raw = json.loads(own.read_text(encoding="utf-8"))
        shifted = False
    else:
        job_words = Path(settings.JOBS_DIR) / str(clip.job_id) / "words.json"
        if not job_words.exists():
            return []
        raw = json.loads(job_words.read_text(encoding="utf-8"))
        raw = [w for w in (raw.get("words", []) if isinstance(raw, dict) else raw)
               if clip.start <= float(w.get("start", 0)) and float(w.get("end", 0)) <= clip.end + 0.05]
        shifted = True
    items = raw.get("words", []) if isinstance(raw, dict) else raw
    out = []
    for w in items:
        text = str(w.get("word") or "").strip()
        if not text:
            continue
        start = float(w.get("start", 0)) - (clip.start if shifted else 0)
        end = float(w.get("end", 0)) - (clip.start if shifted else 0)
        out.append({"i": len(out), "word": text, "start": round(max(0.0, start), 3),
                    "end": round(max(0.0, end), 3)})
    return out


@router.get("/clips/{clip_id}/words")
async def clip_words(clip_id: int, db: Session = Depends(get_database)):
    """The clip's words and its render length, for cutting the clip by its words."""
    from services.trim import _probe_duration_quiet

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    media = _clip_media_path(clip)
    duration = await run_in_threadpool(_probe_duration_quiet, media) if media else 0.0
    trim = None
    if clip.trim_start is not None and clip.trim_end is not None:
        trim = {"start": clip.trim_start, "end": clip.trim_end}
    return {"words": _clip_words(clip), "duration": round(duration, 3), "trim": trim}


@router.get("/clips/{clip_id}/quality")
def clip_quality(clip_id: int, db: Session = Depends(get_database)):
    """The clip's quality report (findings with evidence) and the manifest of how it was rendered."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    return {
        "report": clip.quality_report,
        "manifest": clip.render_manifest,
        "fingerprint": clip.render_fingerprint,
    }


@router.post("/clips/{clip_id}/trim/reset")
async def reset_trim(clip_id: int, db: Session = Depends(get_database)):
    """Drop the trim: the clip goes back to its full render. The trimmed file is kept on disk."""
    from utils import clip_trim

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    base = clip_trim.base_render_path(_clip_dir(clip))
    if not os.path.exists(base):
        raise HTTPException(status_code=400, detail="Rendered clip file not found; re-render the clip first")
    clip.trim_start = None
    clip.trim_end = None
    clip.file_path = os.path.relpath(base, settings.STORAGE_ROOT)
    db.commit()
    db.refresh(clip)
    return {"id": clip.id, "file_path": clip.file_path, "trim": None}
