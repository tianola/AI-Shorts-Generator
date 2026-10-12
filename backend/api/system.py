"""System introspection: caption presets, health, source metadata, hardware estimates.

main.py mounts this router under /api. None of these endpoints touch the database,
which is why they are the easiest to reason about on their own.
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/system/presets")
async def get_system_presets():
    """Return available OpusClip subtitle style presets and multi-layout modes."""
    from services.captions import PRESETS
    presets_list = [
        {
            "id": k,
            "name": v.name,
            "highlight_color": v.highlight_color,
            "primary_color": v.primary_color,
            "jump": v.jump,
            "add_emojis": v.add_emojis
        }
        for k, v in PRESETS.items()
    ]
    layouts_list = [
        {"id": "single_speaker", "name": "Solo Speaker Follow", "description": "Smooth YOLO face tracking"},
        {"id": "split_screen", "name": "Podcast Split (9:16)", "description": "Stacked 2-speaker view"},
        {"id": "fit_blur", "name": "Blurred Background Fit", "description": "Full frame with blurred backdrop"}
    ]
    return {"presets": presets_list, "layouts": layouts_list}


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/video/metadata")
async def get_video_metadata(url: str):
    """Quickly extract video metadata (title, duration, thumbnail) without downloading."""
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    try:
        import yt_dlp
        ydl_opts = {
            "extract_flat": True,
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "socket_timeout": 15,
        }
        if getattr(settings, "YT_COOKIES_FILE", None) and os.path.exists(settings.YT_COOKIES_FILE):
            ydl_opts["cookiefile"] = settings.YT_COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False)
            dur = float(info.get("duration") or 0)
            mins = int(dur // 60)
            secs = int(dur % 60)
            dur_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            return {
                "title": info.get("title") or "Unknown Video",
                "duration": dur,
                "duration_formatted": dur_str,
                "thumbnail": info.get("thumbnail"),
            }
    except Exception as e:
        logger.warning(f"Failed to fetch metadata for {url}: {e}")
        return {
            "title": None,
            "duration": 0,
            "duration_formatted": None,
            "thumbnail": None,
        }


@router.get("/system/info")
async def get_system_info(duration: Optional[float] = None):
    """Get accurate hardware specs and dynamically estimated Whisper models based on video duration."""
    try:
        import psutil
    except ImportError:
        psutil = None
    try:
        import torch
    except ImportError:
        torch = None

    import platform
    import subprocess

    cpu_name = None
    try:
        res = subprocess.run(
            ["wmic", "cpu", "get", "name"],
            capture_output=True, text=True, timeout=5
        )
        lines = [
            line.strip() for line in res.stdout.strip().split("\n")
            if line.strip() and line.strip().lower() != "name"
        ]
        if lines:
            cpu_name = lines[0]
    except Exception:
        pass

    if not cpu_name:
        cpu_name = platform.processor() or "Multi-core CPU"

    threads = psutil.cpu_count(logical=True) if psutil else (os.cpu_count() or 8)
    ram_gb = round(psutil.virtual_memory().total / (1024**3), 1) if psutil else 16.0

    cpu_label = f"{cpu_name} ({threads} Threads)"
    ram_label = f"{ram_gb} GB"

    has_gpu = False
    gpu_name = "None (CPU Multi-threaded)"
    vram_gb = 0.0
    if torch is not None and torch.cuda.is_available():
        has_gpu = True
        gpu_name = torch.cuda.get_device_name(0)
        vram_gb = round(torch.cuda.get_device_properties(0).total_memory / (1024**3), 1)

    # Dynamic speed scaling factors based on real hardware performance
    is_high_end_cpu = threads >= 16 and ram_gb >= 16
    cpu_factor = 0.7 if is_high_end_cpu else (1.0 if threads >= 8 else 1.8)

    models_def = [
        {
            "id": "groq",
            "name": "Groq Whisper Turbo (Cloud ⚡ Instant)",
            "cloud": True,
            "rtf_gpu": 0.01,
            "rtf_cpu": 0.01,
            "vram_req": 0.0,
            "ram_req": 0.0,
        },
        {
            "id": "tiny",
            "name": "Tiny (Local, Ultra-Fast)",
            "cloud": False,
            "rtf_gpu": 0.015,
            "rtf_cpu": 0.05 * cpu_factor,
            "vram_req": 0.6,
            "ram_req": 0.8,
        },
        {
            "id": "base",
            "name": "Base (Local, Fast)",
            "cloud": False,
            "rtf_gpu": 0.025,
            "rtf_cpu": 0.10 * cpu_factor,
            "vram_req": 1.0,
            "ram_req": 1.2,
        },
        {
            "id": "small",
            "name": "Small (Local, Balanced)",
            "cloud": False,
            "rtf_gpu": 0.050,
            "rtf_cpu": 0.22 * cpu_factor,
            "vram_req": 2.0,
            "ram_req": 2.5,
        },
        {
            "id": "medium",
            "name": "Medium (Local, High Accuracy)",
            "cloud": False,
            "rtf_gpu": 0.120,
            "rtf_cpu": 0.55 * cpu_factor,
            "vram_req": 4.5,
            "ram_req": 5.0,
        },
        {
            "id": "large-v3",
            "name": "Large V3 (Local, Best Quality)",
            "cloud": False,
            "rtf_gpu": 0.220,
            "rtf_cpu": 1.10 * cpu_factor,
            "vram_req": 8.0,
            "ram_req": 9.0,
        },
    ]

    # Compute accurate duration-based estimates
    effective_duration = float(duration) if duration and float(duration) > 0 else 300.0  # default 5 min reference
    groq_key = os.getenv("GROQ_API_KEY")

    if groq_key:
        recommended = "groq"
    elif has_gpu and vram_gb >= 8:
        recommended = "medium"
    elif has_gpu and vram_gb >= 4:
        recommended = "small"
    elif is_high_end_cpu:
        recommended = "small"
    elif threads >= 8:
        recommended = "base"
    else:
        recommended = "tiny"

    formatted_models = []
    for m in models_def:
        is_rec = (m["id"] == recommended)
        rtf = m["rtf_gpu"] if has_gpu else m["rtf_cpu"]
        if m["cloud"]:
            est = "~5-10 sec"
        else:
            sec = max(5, int(effective_duration * rtf))
            if sec < 60:
                est = f"~{sec} sec"
            else:
                mins = round(sec / 60, 1)
                est = f"~{int(mins) if mins.is_integer() else mins} min"

        formatted_models.append({
            "id": m["id"],
            "name": m["name"],
            "est_time": est,
            "rtf": rtf,
            "is_cloud": m["cloud"],
            "is_recommended": is_rec,
            "ram_required": m["vram_req"] if has_gpu else m["ram_req"],
        })

    return {
        "hardware": {
            "cpu": cpu_label,
            "ram": ram_label,
            "gpu": gpu_name,
            "vram": f"{vram_gb} GB" if vram_gb > 0 else "N/A",
        },
        "models": formatted_models,
    }

