"""Real-time job progress over WebSocket.

main.py mounts this router under /api, so the socket a client opens is still
/api/ws/progress/{job_id}.
"""
import asyncio
import json
import os

import redis.asyncio as redis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import List

from database.models import Job, JobProgress
from database.session import SessionLocal

router = APIRouter()



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
