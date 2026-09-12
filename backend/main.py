"""
Main FastAPI application for AI Shorts Generator.
"""
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database.session import init_database
from api.routes import router as api_router
from config import settings
import uvicorn

BACKEND_DIR = Path(__file__).resolve().parent
STATIC_DIR = BACKEND_DIR / "static"
TEMPLATES_DIR = BACKEND_DIR / "templates"

# Create FastAPI app
app = FastAPI(
    title="AI Shorts Generator API",
    description="API for generating YouTube Shorts from long-form videos",
    version="2.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Include API routes
app.include_router(api_router, prefix="/api")

# Serve UI assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Initialize database on startup
@app.on_event("startup")
def startup_event():
    """Initialize database tables on startup."""
    init_database()

@app.get("/")
async def root():
    """Serve the Shorts Studio dashboard."""
    return FileResponse(TEMPLATES_DIR / "index.html")

@app.get("/media/{file_path:path}")
async def media_proxy(file_path: str):
    """Serve generated media files and thumbnails for the UI."""
    safe_root = Path(settings.STORAGE_ROOT).resolve()
    safe_path = (safe_root / file_path).resolve()

    if safe_root not in safe_path.parents and safe_path != safe_root:
        raise FileNotFoundError("Invalid media path")

    if not safe_path.exists():
        raise FileNotFoundError(f"Media not found: {file_path}")

    return FileResponse(safe_path)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=False
    )