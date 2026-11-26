"""The studio's view of how a clip was edited: the timeline, why, the quality record, and an A/B check."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from config import settings
from database.models import Clip
from database.session import get_database

router = APIRouter()

TIMELINE_FILE = "edit_timeline.json"
PLAIN_FILE = "final_plain.mp4"
FINAL_FILE = "final_video.mp4"


def _clip_dir(clip: Clip) -> Path:
    return Path(settings.JOBS_DIR) / str(clip.job_id) / f"clip_{clip.index}"


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _media(path: Path) -> Optional[str]:
    """Relative storage path of an existing file, or None."""
    if not path.is_file():
        return None
    return os.path.relpath(str(path), settings.STORAGE_ROOT).replace("\\", "/")


def _speed(clip: Clip) -> float:
    antifp = (clip.render_manifest or {}).get("anti_fingerprint") or {}
    try:
        return float(antifp.get("speed")) if antifp.get("applied") else 1.0
    except (TypeError, ValueError):
        return 1.0


@router.get("/clips/{clip_id}/edit")
def clip_edit_view(clip_id: int, db: Session = Depends(get_database)):
    """Everything the studio shows about the edit: timeline with reasons, quality, plain copy, A/B result."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    clip_dir = _clip_dir(clip)
    timeline = _read_json(clip_dir / TIMELINE_FILE)
    explanations = []
    if timeline:
        try:
            from services.editing.describe import explain
            from services.editing.models import Timeline
            explanations = explain(Timeline.model_validate(timeline))
        except Exception:
            explanations = []
    from services.editing.ab import AB_FILE
    return {
        "clip_id": clip.id,
        "profile": clip.edit_profile,
        "tone": clip.tone_category,
        "speed": _speed(clip),
        "timeline": timeline,
        "explanations": explanations,
        "quality": clip.quality_report,
        "fingerprint": clip.render_fingerprint,
        "final_url": _media(clip_dir / FINAL_FILE),
        "plain_url": _media(clip_dir / PLAIN_FILE),
        "ab": _read_json(clip_dir / AB_FILE),
    }


def _run_ab(clip_dir: Path, timeline: dict, speed: float) -> dict:
    from services.editing import ab
    work = tempfile.mkdtemp(prefix="ab_")
    try:
        report = ab.ab_report(
            str(clip_dir / PLAIN_FILE), str(clip_dir / FINAL_FILE),
            timeline.get("visual_tracks") or [], float(timeline.get("clip_duration") or 0.0),
            speed, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ab.write_report(str(clip_dir), report)
    return report


@router.post("/clips/{clip_id}/ab-check")
async def clip_ab_check(clip_id: int, db: Session = Depends(get_database)):
    """Compare the plain copy with the finished clip at every planned visual effect."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")
    clip_dir = _clip_dir(clip)
    timeline = _read_json(clip_dir / TIMELINE_FILE)
    if not timeline:
        raise HTTPException(status_code=409, detail="This clip has no edit timeline")
    if not (clip_dir / PLAIN_FILE).is_file() or not (clip_dir / FINAL_FILE).is_file():
        raise HTTPException(status_code=409, detail="No plain copy of this clip. Turn on RENDER_PLAIN_PREVIEW and render again.")
    if not timeline.get("visual_tracks"):
        raise HTTPException(status_code=409, detail="This clip has no visual effects to check")
    return await run_in_threadpool(_run_ab, clip_dir, timeline, _speed(clip))
