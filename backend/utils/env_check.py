"""Startup environment checks.

Every render stage shells out to ffmpeg/ffprobe. Before this check a missing
binary surfaced only as a failed job with a bare FileNotFoundError from deep
inside the subprocess layer, while the API itself looked perfectly healthy.
"""
import shutil
import subprocess

from loguru import logger

_MEDIA_TOOLS = ("ffmpeg", "ffprobe")


def missing_media_tools() -> list:
    """Media tools that are not on PATH."""
    return [tool for tool in _MEDIA_TOOLS if shutil.which(tool) is None]


def log_media_tools() -> list:
    """Log the resolved media tools, and return the missing ones.

    The API still serves jobs, clips and settings without ffmpeg, so this
    reports the problem instead of refusing to start.
    """
    missing = missing_media_tools()
    for tool in missing:
        logger.error(
            f"{tool} is not on PATH - video jobs cannot render. "
            "Install FFmpeg and restart the API and the worker."
        )
    for tool in _MEDIA_TOOLS:
        if tool in missing:
            continue
        try:
            proc = subprocess.run(
                [tool, "-version"], capture_output=True, text=True, timeout=10
            )
            first_line = proc.stdout.splitlines()[0] if proc.stdout else "version unknown"
            logger.info(f"{tool}: {first_line}")
        except Exception as exc:
            logger.warning(f"{tool} is on PATH but did not run: {exc}")
    return missing
