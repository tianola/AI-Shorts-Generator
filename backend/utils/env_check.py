"""Startup environment checks.

Every render stage shells out to ffmpeg/ffprobe. Before this check a missing
binary surfaced only as a failed job with a bare FileNotFoundError from deep
inside the subprocess layer, while the API itself looked perfectly healthy.
"""
import glob
import os
import shutil
import subprocess

from loguru import logger

_MEDIA_TOOLS = ("ffmpeg", "ffprobe")


def _candidate_media_dirs() -> list:
    """Folders where FFmpeg usually lives on a Windows box, best first.

    The NSSM services run as LocalSystem, whose PATH and LOCALAPPDATA do not
    include the per-user WinGet folders where FFmpeg was installed. The API
    service then logged "ffmpeg is not on PATH" on every start while the worker,
    which had a PATH override in its service config, rendered fine.
    """
    dirs = []
    explicit = os.environ.get("FFMPEG_DIR", "").strip()
    if explicit:
        dirs.append(explicit)
    system_drive = os.environ.get("SystemDrive", "C:") + os.sep
    users_root = os.path.join(system_drive, "Users")
    winget_roots = glob.glob(os.path.join(users_root, "*", "AppData", "Local", "Microsoft", "WinGet"))
    for root in winget_roots:
        dirs.extend(sorted(glob.glob(os.path.join(root, "Packages", "*FFmpeg*", "*", "bin")), reverse=True))
        dirs.append(os.path.join(root, "Links"))
    program_files = os.environ.get("ProgramFiles", os.path.join(system_drive, "Program Files"))
    dirs.append(os.path.join(program_files, "ffmpeg", "bin"))
    dirs.append(os.path.join(system_drive, "ffmpeg", "bin"))
    return dirs


def _has_media_tools(folder: str) -> bool:
    return all(shutil.which(tool, path=folder) for tool in _MEDIA_TOOLS)


def ensure_media_tools_on_path() -> str:
    """Put an FFmpeg folder on this process's PATH when ffmpeg is not already on it.

    Subprocesses inherit os.environ, so every later ffmpeg/ffprobe call finds it.
    Returns the folder added, or "" when nothing changed.
    """
    if not [tool for tool in _MEDIA_TOOLS if shutil.which(tool) is None]:
        return ""
    if os.name != "nt":
        return ""
    for folder in _candidate_media_dirs():
        if os.path.isdir(folder) and _has_media_tools(folder):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            logger.info(f"ffmpeg was not on PATH; using {folder}")
            return folder
    return ""


def missing_media_tools() -> list:
    """Media tools that are not on PATH."""
    ensure_media_tools_on_path()
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
