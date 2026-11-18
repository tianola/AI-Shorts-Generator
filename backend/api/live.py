"""Live Studio API."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from services import live_studio

router = APIRouter()


class ConnectRequest(BaseModel):
    source: str = "youtube"
    url: str = ""
    sensitivity: int = 5
    buffer_minutes: int = 10
    auto_clip: bool = True


class InstantRequest(BaseModel):
    seconds: int = 30


def _active() -> live_studio.LiveSession:
    session = live_studio.get_session()
    if session is None:
        raise HTTPException(status_code=409, detail="No live session. Connect first")
    return session


@router.get("/live/status")
async def status():
    session = live_studio.get_session()
    if session is None:
        return {"status": "idle", "detail": "Ready", "elapsed": 0, "transcript": [], "clips": []}
    return {"status": session.status, "detail": session.detail, "elapsed": round(session.elapsed),
            "transcript": session.transcript[-12:], "clips": session.clips()}


@router.get("/live/obs")
async def obs_config():
    return {"server": "rtmp://127.0.0.1:1935/live", "stream_key": "stream",
            "instructions": "OBS: Settings > Stream > Service Custom, Server rtmp://127.0.0.1:1935/live, Stream Key stream, then Start Streaming."}


@router.post("/live/connect")
async def connect(req: ConnectRequest):
    try:
        live_studio.start_session(req.source, req.url, sensitivity=req.sensitivity,
                                  buffer_minutes=req.buffer_minutes, auto_clip=req.auto_clip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "connecting"}


@router.post("/live/disconnect")
async def disconnect():
    live_studio.stop_session()
    return {"status": "idle"}


@router.post("/live/instant-clip")
async def instant_clip(req: InstantRequest):
    session = _active()
    try:
        return {"clip": await run_in_threadpool(session.instant_clip, req.seconds)}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/live/clips/{clip_id}")
async def delete_clip(clip_id: str):
    session = _active()
    if not session.delete_clip(clip_id):
        raise HTTPException(status_code=404, detail="Unknown clip")
    return {"deleted": clip_id}
