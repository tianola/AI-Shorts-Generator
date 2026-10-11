"""
Structured logging configuration using loguru.
Logs to stdout (Docker) and to job-specific pipeline.log files.
"""
import sys
import os
from pathlib import Path
from loguru import logger
from typing import Optional
from config import settings

# Track added file handlers to avoid duplicates
_added_handlers = set()

def get_job_logger(job_id: int, clip_id: Optional[int] = None):
    """
    Get a logger that writes to stdout and to this specific job's pipeline.log.

    Args:
        job_id: The job ID (> 0)
        clip_id: Optional clip ID for more specific context

    Returns:
        A loguru logger with job context
    """
    global _added_handlers

    # Per-job pipeline.log ONLY for real jobs (job_id > 0)
    if job_id and job_id > 0:
        job_dir = Path(settings.JOBS_DIR) / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(job_dir / "pipeline.log")

        if log_file not in _added_handlers:
            target_job_id = int(job_id)
            logger.add(
                log_file,
                level="DEBUG",
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra} | {message}",
                filter=lambda record, jid=target_job_id: record["extra"].get("job_id") == jid,
                enqueue=True,
                rotation="10 MB",
                retention="30 days"
            )
            _added_handlers.add(log_file)

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