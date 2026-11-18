"""Logo/text branding: the saved config, the logo upload, and applying it to a clip."""
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from config import settings
from database.models import Clip
from database.session import get_database
from services import branding

router = APIRouter()

_LOGO_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_LOGO_BYTES = 8 * 1024 * 1024


@router.get("/branding")
async def get_branding():
    return {"branding": branding.load()}


@router.put("/branding")
async def put_branding(payload: dict):
    return {"branding": branding.save(payload)}


@router.post("/branding/logo")
async def upload_logo(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _LOGO_EXT:
        raise HTTPException(status_code=400, detail=f"Logo must be one of {', '.join(sorted(_LOGO_EXT))}")
    data = await file.read(_MAX_LOGO_BYTES + 1)
    if len(data) > _MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo is larger than 8 MB")
    dest = branding.logo_dir() / f"logo-{uuid.uuid4().hex[:8]}{ext}"
    dest.write_bytes(data)
    cfg = branding.load()
    cfg.update(logo_path=str(dest), logo_enabled=True)
    return {"branding": branding.save(cfg)}


@router.post("/clips/{clip_id}/branding")
async def brand_clip(clip_id: int, db: Session = Depends(get_database)):
    """Burn the saved branding into this clip's finished video as branded.mp4 beside it."""
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip or not clip.file_path:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} has no rendered video")
    src = Path(settings.STORAGE_ROOT) / clip.file_path
    if not src.is_file():
        raise HTTPException(status_code=404, detail="Rendered video file is missing")
    dst = src.with_name("branded.mp4")
    try:
        done = await run_in_threadpool(branding.apply_branding, str(src), str(dst))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not done:
        raise HTTPException(status_code=400, detail="Branding is empty: add a logo or text first")
    return {"path": str(dst.relative_to(settings.STORAGE_ROOT)).replace("\\", "/")}
