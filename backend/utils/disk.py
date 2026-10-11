"""Free-space checks for the volume that holds job files.

A single multi-hour source video is tens of gigabytes (job 80 downloaded
17 GB), and a download that runs the volume out writes a truncated file,
corrupts the SQLite database sitting on the same drive and takes the API down
with it. The worker therefore asks the same question before claiming work:
is there enough room to finish?
"""
import os
import shutil

from loguru import logger

from config import settings


class NotEnoughSpaceError(RuntimeError):
    """Raised when a write would take the storage volume below its margin."""


def free_bytes(path: str | None = None) -> int:
    """Bytes available on the volume holding `path` (defaults to STORAGE_ROOT)."""
    return shutil.disk_usage(path or settings.STORAGE_ROOT).free


def free_gb(path: str | None = None) -> float:
    return free_bytes(path) / 1024**3


def has_headroom(path: str | None = None) -> bool:
    """True when the volume still has MIN_FREE_DISK_GB free.

    The margin is a floor rather than an estimate of one download: yt-dlp only
    learns a filesize once the manifest is fetched, and the point of the margin
    is to leave the drive usable for the database and the rendered clips.
    """
    checked = path or settings.STORAGE_ROOT
    free = free_gb(checked)
    needed = settings.MIN_FREE_DISK_GB
    if free >= needed:
        return True
    logger.warning(
        f"Low disk space on {checked}: {free:.1f} GB free, {needed:.1f} GB "
        "required. Jobs stay queued until space is reclaimed."
    )
    return False


def reclaim(paths: list[str]) -> int:
    """Delete files, ignoring ones that are missing or locked. Bytes freed."""
    freed = 0
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        try:
            os.remove(path)
        except OSError as exc:
            logger.warning(f"Could not remove {path}: {exc}")
            continue
        freed += size
        logger.info(f"Reclaimed {size / 1024**2:.1f} MB: {path}")
    return freed
