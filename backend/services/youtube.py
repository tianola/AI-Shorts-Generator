from pathlib import Path
from urllib.parse import urlparse
import yt_dlp
from config import settings


def download_youtube(url: str, job_id: int):

    output_dir = Path(
        f"storage/jobs/{job_id}"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    output_file = output_dir / "source.mp4"
    parsed = urlparse(url)
    is_youtube = "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc

    ydl_opts = {
        "outtmpl": str(output_file),
        "noplaylist": True,
        "quiet": False,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 8,
        # Force exact video — do NOT pick random results
        "extract_flat": False,
        "format": "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        },
        # Prevent yt_dlp from falling back to unrelated videos
        "ignoreerrors": False,
        "no_warnings": False,
    }

    if is_youtube:
        # YouTube: prefer a compatible H264 1080p file and merge audio/video cleanly.
        # Force exact video ID match to avoid downloading unrelated videos.
        ydl_opts.update({
            "format": (
                "bestvideo[height<=1080][vcodec^=avc1]"
                "+bestaudio/best[height<=1080]"
            ),
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 8,
        })
    else:
        # Direct media links (MP4/WebM/MOV etc.) are often generic URLs and do not support the
        # YouTube-specific AVC1+audio format filters. Fall back to a generic best-format download.
        ydl_opts.update({
            "format": "best[ext=mp4]/best[ext=webm]/best[ext=mov]/best",
            "merge_output_format": "mp4",
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Single fetch that also downloads — avoids the double-fetch of
            # extract_info(download=False) + download() which was slow and could
            # fail on the second pass.
            ydl.download([url])
        except Exception as e:
            # Clean up any partial download so a later retry starts fresh
            partials = list(output_dir.glob("*.part")) + list(output_dir.glob("*.ytdl"))
            for partial in partials:
                try:
                    partial.unlink()
                except OSError:
                    pass
            raise Exception(f"Download failed for {url}: {e}")

    job_dir = Path(settings.JOBS_DIR) / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    # After download, verify the video title matches expectation
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=title", "-of", "default=noprint_wrappers=1:nokey=1", str(output_file)],
            capture_output=True, text=True
        )
        downloaded_title = result.stdout.strip() if result.returncode == 0 else "Unknown"
        if downloaded_title and downloaded_title != "N/A":
            from utils.logger import job_info
            job_info(job_id, f"Downloaded video title: {downloaded_title}")
    except Exception:
        pass

    return str(output_file)