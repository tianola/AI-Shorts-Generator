"""
Main FastAPI application for AI Shorts Generator.
"""
import sys
import os

# Must run before any import that pulls in loguru: see utils/daemon_streams.py
from utils.daemon_streams import redirect_daemon_streams

redirect_daemon_streams("api")

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database.session import init_database
from utils.env_check import log_media_tools
from api.routes import router as api_router
from api.features import router as features_router
from config import settings
import uvicorn

BACKEND_DIR = Path(__file__).resolve().parent
STATIC_DIR = BACKEND_DIR / "static"
TEMPLATES_DIR = BACKEND_DIR / "templates"

ALLOWED_MEDIA_EXTENSIONS = {
    ".mp4", ".webm", ".mov", ".m4v", ".mkv",
    ".jpg", ".jpeg", ".png", ".webp", ".gif"
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for application startup and shutdown."""
    init_database()
    log_media_tools()
    yield

# Create FastAPI app
app = FastAPI(
    title="AI Shorts Generator API",
    description="API for generating YouTube Shorts from long-form videos",
    version="2.0.0",
    lifespan=lifespan
)

# CORS middleware
# "*" alongside allow_credentials=True is rejected by browsers and, more
# importantly, lets any page on the LAN reach this unauthenticated API.
_cors_origins = settings.cors_origin_list
_allow_wildcard = "*" in _cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_wildcard else _cors_origins,
    allow_credentials=not _allow_wildcard,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Include API routes
app.include_router(api_router, prefix="/api")
app.include_router(features_router, prefix="/api")

# Serve UI assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    """Serve the Shorts Studio dashboard."""
    return FileResponse(TEMPLATES_DIR / "index.html")

@app.get("/media/{file_path:path}")
async def media_proxy(file_path: str):
    """Serve generated media files and thumbnails for the UI securely."""
    safe_root = Path(settings.STORAGE_ROOT).resolve()
    safe_path = (safe_root / file_path).resolve()

    if safe_root not in safe_path.parents and safe_path != safe_root:
        raise HTTPException(status_code=403, detail="Access denied")

    # Disallow access to secrets, database files, and sensitive extensions
    normalized_path = str(safe_path).replace("\\", "/").lower()
    if "/secrets/" in normalized_path or any(part.startswith(".") for part in safe_path.parts):
        raise HTTPException(status_code=403, detail="Access to sensitive resources is forbidden")

    ext = safe_path.suffix.lower()
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=403, detail="File type not allowed")

    if not safe_path.exists() or not safe_path.is_file():
        raise HTTPException(status_code=404, detail=f"Media not found: {file_path}")

    return FileResponse(safe_path)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=False
    )