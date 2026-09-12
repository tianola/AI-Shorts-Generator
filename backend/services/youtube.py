from pathlib import Path
from urllib.parse import urlparse
import yt_dlp


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
        "format": "best[ext=mp4]/best",  # Simplify format selection to avoid 403 on some endpoints
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }
    }

    if is_youtube:
        # YouTube: prefer a compatible H264 1080p file and merge audio/video cleanly.
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
        ydl.download([url])

    return str(output_file)