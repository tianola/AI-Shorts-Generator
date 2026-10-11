"""Stdio redirection for windowless/background daemon runs.

pythonw and detached launches have sys.stdout/sys.stderr == None, so the worker
used to point them at plain append-mode files in storage/logs. Nothing ever
rotated those files and loguru's stderr sink wrote to them on every message, so
one of them reached 1.2 GB while the worker was processing nothing.

Imported before anything that pulls in loguru, because configure_logging() adds
a stderr sink at import time and that fails while sys.stderr is None. Only the
standard library and config may be imported here.
"""
import os
import sys

from config import settings

MAX_DAEMON_LOG_BYTES = 20 * 1024 * 1024
KEEP_GENERATIONS = 1


def _rotate_oversized(path: str) -> None:
    try:
        if os.path.getsize(path) <= MAX_DAEMON_LOG_BYTES:
            return
        for generation in range(KEEP_GENERATIONS - 1, 0, -1):
            older = f"{path}.{generation}"
            if os.path.exists(older):
                os.replace(older, f"{path}.{generation + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        # A log we cannot rotate is not worth crashing a job over.
        pass


def redirect_daemon_streams(prefix: str) -> None:
    """Send a daemon's missing stdout/stderr to capped files, or /dev/null."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(settings.LOGS_DIR, exist_ok=True)
    except OSError:
        sys.stdout = sys.stdout or open(os.devnull, "w")
        sys.stderr = sys.stderr or open(os.devnull, "w")
        return

    for stream_name in ("stdout", "stderr"):
        if getattr(sys, stream_name) is not None:
            continue
        suffix = "" if stream_name == "stdout" else "_error"
        path = os.path.join(settings.LOGS_DIR, f"{prefix}{suffix}.log")
        try:
            if os.path.exists(path):
                _rotate_oversized(path)
            setattr(sys, stream_name, open(path, "a", encoding="utf-8"))
        except OSError:
            setattr(sys, stream_name, open(os.devnull, "w"))
