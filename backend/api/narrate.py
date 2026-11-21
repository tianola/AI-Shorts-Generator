"""Narrate Studio API: voices, footage library, script writing and rendering."""
import json
import os
import threading
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import settings
from services import narrate

router = APIRouter()

_MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB


class ScriptRequest(BaseModel):
    idea: str
    style: str = "story"
    seconds: int = 40
    language: str = ""


class RenderRequest(BaseModel):
    script: str
    voice: str = ""
    background: Optional[str] = None
    rate: int = 0
    caption_style: str = "mrbeast"


def _status_file(job_id: str):
    return narrate.root() / job_id / "status.json"


def _write_status(job_id: str, **fields) -> None:
    path = _status_file(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    current.update(fields)
    path.write_text(json.dumps(current), encoding="utf-8")


@router.get("/narrate/voices")
async def voices():
    return {"voices": await run_in_threadpool(narrate.list_voices)}


@router.get("/narrate/library")
async def library():
    return {"videos": narrate.list_library()}


@router.post("/narrate/library")
async def add_to_library(file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "")
    if not name or os.path.splitext(name)[1].lower() not in narrate.VIDEO_EXT or name.startswith("."):
        raise HTTPException(status_code=400, detail="Upload an mp4, mov, mkv, webm or m4v video")
    dest = narrate.library_dir() / name
    written = 0
    with open(dest, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > _MAX_UPLOAD:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Video is larger than 1 GB")
            fh.write(chunk)
    return {"videos": narrate.list_library()}


@router.post("/narrate/script")
async def write_script(req: ScriptRequest):
    try:
        return {"script": await narrate.generate_script(req.idea, req.style, req.seconds, req.language)}
    except narrate.NarrateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/narrate/render")
async def start_render(req: RenderRequest):
    if len(req.script.split()) < 3:
        raise HTTPException(status_code=400, detail="The script is empty")
    if req.background:
        try:
            narrate.safe_library_file(req.background)
        except narrate.NarrateError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    job_id = narrate.new_job_id()
    job_dir = narrate.root() / job_id
    _write_status(job_id, state="running", message="Starting", percent=0)

    def work():
        try:
            final = narrate.render(job_dir, req.script, req.voice, req.background, req.rate,
                                   req.caption_style,
                                   progress=lambda m, p: _write_status(job_id, message=m, percent=p))
            rel = os.path.relpath(final, settings.STORAGE_ROOT).replace("\\", "/")
            _write_status(job_id, state="done", message="Done", percent=100, path=rel)
        except Exception as exc:  # the thread must record the failure, not die silently
            _write_status(job_id, state="failed", message=str(exc)[:400])

    threading.Thread(target=work, name=f"narrate-{job_id}", daemon=True).start()
    return {"job_id": job_id}


@router.get("/narrate/status/{job_id}")
async def render_status(job_id: str):
    if not job_id.isalnum():
        raise HTTPException(status_code=404, detail="Unknown narration")
    try:
        return json.loads(_status_file(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Unknown narration")
