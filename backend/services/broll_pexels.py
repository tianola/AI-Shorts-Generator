"""
Service to inject external stock video (B-Roll) from Pexels/Pixabay over an existing video timeline.
"""
import os
import subprocess
import httpx
from typing import Optional
from loguru import logger
from config import settings

def fetch_and_overlay_broll(
    base_video: str,
    output_path: str,
    query: str,
    start_time: float = 2.0,
    duration: float = 3.5,
    opacity: float = 0.95
) -> Optional[str]:
    """
    Search Pexels for a stock video query, download, trim/scale, and overlay it 
    onto the base video timeline without cutting base audio.
    """
    pexels_key = os.getenv("PEXELS_API_KEY") or getattr(settings, "PEXELS_API_KEY", None)
    if not pexels_key or not query:
        return None

    try:
        logger.info(f"Searching Pexels B-roll for '{query}' (overlay @ {start_time}s for {duration}s)")
        headers = {"Authorization": pexels_key}
        params = {"query": query, "per_page": 3, "orientation": "portrait", "size": "medium"}

        r = httpx.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=10.0)
        if r.status_code != 200:
            logger.warning(f"Pexels search failed: {r.status_code}")
            return None

        videos = r.json().get("videos", [])
        if not videos:
            logger.info(f"No Pexels videos found for query: {query}")
            return None

        # Pick best portrait file
        video_files = videos[0].get("video_files", [])
        hd_files = [v for v in video_files if v.get("width") and v.get("width") <= 1080]
        chosen = hd_files[0] if hd_files else (video_files[0] if video_files else None)
        if not chosen or not chosen.get("link"):
            return None

        temp_broll = output_path + ".temp_broll.mp4"
        download_resp = httpx.get(chosen["link"], timeout=45.0)
        if download_resp.status_code != 200:
            return None

        with open(temp_broll, "wb") as f:
            f.write(download_resp.content)

        # Overlay the B-Roll video on top of the main vertical video
        # We scale the B-Roll to 1080x1920 with crop/pad, fade in/out slightly, and enable it only between start_time and start_time+duration
        end_time = start_time + duration
        
        # Complex filter:
        # 1. Scale/crop B-Roll to 1080x1920
        # 2. Add subtle fade in / fade out
        # 3. Overlay on main stream
        filter_complex = (
            f"[1:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            f"format=yuva420p,"
            f"fade=t=in:st=0:d=0.3:alpha=1,fade=t=out:st={duration-0.3}:d=0.3:alpha=1[broll];"
            f"[0:v][broll]overlay=0:0:enable='between(t,{start_time},{end_time})'[outv]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", base_video,
            "-ss", "0", "-t", str(duration), "-i", temp_broll,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", settings.FFMPEG_PRESET,
            "-crf", str(settings.FFMPEG_CRF),
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path
        ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and os.path.exists(output_path):
            logger.info(f"Successfully applied B-roll overlay to {output_path}")
            return output_path
        else:
            logger.warning(f"FFmpeg B-roll overlay failed: {res.stderr}")
            return None

    except Exception as e:
        logger.warning(f"B-roll processing error: {e}")
        return None
    finally:
        temp_broll = output_path + ".temp_broll.mp4"
        if os.path.exists(temp_broll):
            try:
                os.remove(temp_broll)
            except Exception:
                pass
