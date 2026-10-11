"""
Structured logging configuration using loguru.

Sinks:
  * ``<storage>/logs/app.log`` — rotating, retention-limited, the full stream.
  * stderr — WARNING and above only, so a crash is still visible on the console
    (in windowless daemon mode stderr is redirected to a file nothing rotates).
  * ``<storage>/jobs/<id>/pipeline.log`` — per job, DEBUG, rotating.

The per-job handlers are kept in an LRU and evicted past a cap: every added
sink stays registered for the life of the process, so an unbounded set of them
leaks file handles and makes every log record walk all of them.
"""
import os
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings

MAX_JOB_HANDLERS = 32
APP_LOG_ROTATION = "20 MB"
APP_LOG_RETENTION = "14 days"

_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra} | {message}"
_CONSOLE_FORMAT = "{time:HH:mm:ss} | {level: <8} | {message}"

_configured = False
_job_handlers: "OrderedDict[int, int]" = OrderedDict()


def configure_logging() -> None:
    """Replace loguru's unrotated default stderr sink with bounded sinks."""
    global _configured
    if _configured:
        return
    _configured = True

    logger.remove()

    try:
        os.makedirs(settings.LOGS_DIR, exist_ok=True)
        logger.add(
            os.path.join(settings.LOGS_DIR, "app.log"),
            level=settings.LOG_LEVEL,
            format=_FILE_FORMAT,
            rotation=APP_LOG_ROTATION,
            retention=APP_LOG_RETENTION,
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
    except OSError as exc:
        logger.warning(f"Could not open application log: {exc}")

    logger.add(
        sys.stderr,
        level=settings.LOG_CONSOLE_LEVEL,
        format=_CONSOLE_FORMAT,
        enqueue=True,
        diagnose=False,
    )


configure_logging()


def _drop_stale_job_handler(job_id: int) -> None:
    """Forget a job's handler so a re-run can register a fresh sink."""
    handler_id = _job_handlers.pop(job_id, None)
    if handler_id is not None:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass


def get_job_logger(job_id: int, clip_id: Optional[int] = None):
    """
    Get a logger that writes to the application log and to this job's pipeline.log.

    Args:
        job_id: The job ID (> 0)
        clip_id: Optional clip ID for more specific context

    Returns:
        A loguru logger with job context
    """
    # Per-job pipeline.log ONLY for real jobs (job_id > 0)
    if job_id and job_id > 0:
        target_job_id = int(job_id)
        if target_job_id in _job_handlers:
            _job_handlers.move_to_end(target_job_id)
        else:
            job_dir = Path(settings.JOBS_DIR) / str(target_job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            handler_id = logger.add(
                str(job_dir / "pipeline.log"),
                level="DEBUG",
                format=_FILE_FORMAT,
                filter=lambda record, jid=target_job_id: record["extra"].get("job_id") == jid,
                enqueue=True,
                rotation="10 MB",
                retention="14 days",
            )
            _job_handlers[target_job_id] = handler_id
            while len(_job_handlers) > MAX_JOB_HANDLERS:
                _drop_stale_job_handler(next(iter(_job_handlers)))

    context_clip_id = clip_id if clip_id is not None else "n/a"
    return logger.bind(job_id=job_id if job_id and job_id > 0 else "system", clip_id=context_clip_id)


def log_job_event(job_id: int, message: str, level: str = "INFO", clip_id: Optional[int] = None):
    """
    Log an event for a specific job. If job_id <= 0, logs as a system event.
    """
    # If job_id is 0/None (e.g. idle polling or generic utility), log to console only without polluting job logs
    if not job_id or job_id <= 0:
        sys_logger = logger.bind(job_id="system", clip_id="n/a")
        getattr(sys_logger, level.lower())(message)
        return

    job_logger = get_job_logger(job_id, clip_id)
    getattr(job_logger, level.lower())(message)

# Convenience functions
def job_debug(job_id: int, message: str, clip_id: Optional[int] = None):
    log_job_event(job_id, message, "DEBUG", clip_id)

def job_info(job_id: int, message: str, clip_id: Optional[int] = None):
    log_job_event(job_id, message, "INFO", clip_id)

def job_warning(job_id: int, message: str, clip_id: Optional[int] = None):
    log_job_event(job_id, message, "WARNING", clip_id)

def job_error(job_id: int, message: str, clip_id: Optional[int] = None):
    log_job_event(job_id, message, "ERROR", clip_id)

def job_critical(job_id: int, message: str, clip_id: Optional[int] = None):
    log_job_event(job_id, message, "CRITICAL", clip_id)
