from pathlib import Path
from typing import Optional, Callable
from urllib.parse import urlparse
import os
import yt_dlp
from config import settings


def download_youtube(url: str, job_id: int, progress_callback: Optional[Callable[[str, int], None]] = None):
    output_dir = Path(settings.JOBS_DIR) / str(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    target_file = output_dir / "source.mp4"
    output_tmpl = str(output_dir / "source.%(ext)s")
    parsed = urlparse(url)
    is_youtube = "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc

    # Base options for YoutubeDL
    base_ydl_opts = {
        "outtmpl": output_tmpl,
        "noplaylist": True,
        "quiet": False,
        "socket_timeout": 30,
        "extract_flat": False,
        "merge_output_format": "mp4",
        "ignoreerrors": False,
        "no_warnings": False,
    }

    if is_youtube:
        # Prefer fast direct single HTTPS streams (avoids slow m3u8 fragments throttling)
        base_ydl_opts.update({
            "format": "bestvideo[protocol=https][height<=1080]+bestaudio[protocol=https]/bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[ext=mp4][height<=1080]/best[height<=1080]/best",
        })
    else:
        # Direct media links (MP4/WebM/MOV etc.)
        base_ydl_opts.update({
            "format": "best[ext=mp4]/best[ext=webm]/best[ext=mov]/best",
        })

    # Prepare cookie candidates (same for all attempts)
    cookie_candidates = [
        getattr(settings, "YT_COOKIES_FILE", None),
        os.path.join(settings.STORAGE_ROOT, "secrets", "cookies.txt"),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "storage", "secrets", "cookies.txt")),
    ]
    cookies_path = None
    for cpath in cookie_candidates:
        if cpath and os.path.exists(cpath) and os.path.getsize(cpath) > 0:
            cookies_path = str(cpath)
            break

    # Client configurations to try (in order of preference)
    clients = ['web', 'android', 'ios']
    user_agents = {
        'web': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        'android': 'Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36',
        'ios': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
    }

    last_exception = None
    for client in clients:
        ydl_opts = base_ydl_opts.copy()
        ydl_opts['extractor_args'] = {'youtube': {'player_client': client}}
        ydl_opts['user_agent'] = user_agents[client]

        # Apply cookie logic if cookies are found
        if cookies_path:
            ydl_opts['cookiefile'] = cookies_path
            # Override user_agent to match iPhone (as in original cookie logic)
            ydl_opts['user_agent'] = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

        if progress_callback:
            _last_hook_pct = [-1]
            def yt_progress_hook(d):
                if d.get("status") == "downloading":
                    downloaded = d.get("downloaded_bytes", 0)
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                    dl_pct = max(1, min(99, int((downloaded / total) * 100)))
                    if dl_pct != _last_hook_pct[0]:
                        _last_hook_pct[0] = dl_pct
                        overall_pct = int(2 + (dl_pct * 0.18))
                        progress_callback(f"Downloading video ({dl_pct}%)", overall_pct)
                elif d.get("status") == "finished":
                    progress_callback("Video downloaded (100%)", 20)

            ydl_opts["progress_hooks"] = [yt_progress_hook]

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    meta_file = output_dir / "meta.json"
                    try:
                        import json
                        with open(meta_file, "w", encoding="utf-8") as mf:
                            json.dump({
                                "title": info.get("title", ""),
                                "description": info.get("description", ""),
                                "tags": info.get("tags", []),
                                "channel": info.get("uploader", "") or info.get("channel", ""),
                            }, mf, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                # If we get here, download succeeded
                break
        except Exception as e:
            last_exception = e
            # Clean up any partial download so a later retry starts fresh
            partials = list(output_dir.glob("*.part*")) + list(output_dir.glob("*.ytdl"))
            for partial in partials:
                try:
                    partial.unlink()
                except OSError:
                    pass
            # Continue to next client
            continue
    else:
        # All clients failed
        if last_exception:
            raise Exception(f"Download failed for {url}: {last_exception}")
        else:
            raise Exception(f"Download failed for {url}: Unknown error")

    # Ensure source.mp4 exists (or rename if downloaded as source.mkv / source.webm)
    if not target_file.exists():
        candidates = list(output_dir.glob("source.*"))
        video_candidates = [c for c in candidates if c.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")]
        if video_candidates and video_candidates[0] != target_file:
            video_candidates[0].rename(target_file)

    # After download, verify the video title matches expectation
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=title", "-of", "default=noprint_wrappers=1:nokey=1", str(target_file)],
            capture_output=True, text=True
        )
        downloaded_title = result.stdout.strip() if result.returncode == 0 else "Unknown"
        if downloaded_title and downloaded_title != "N/A":
            from utils.logger import job_info
            job_info(job_id, f"Downloaded video title: {downloaded_title}")
    except Exception:
        pass

    return str(target_file)