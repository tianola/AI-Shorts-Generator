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
    Get a logger that writes to both stdout and job-specific pipeline.log.

    Args:
        job_id: The job ID
        clip_id: Optional clip ID for more specific context

    Returns:
        A loguru logger with job context
    """
    global _added_handlers

    # Create job directory if it doesn't exist
    job_dir = Path(settings.JOBS_DIR) / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    # Log file path
    log_file = str(job_dir / "pipeline.log")

    # Add file handler if not already added for this job
    if log_file not in _added_handlers:
        logger.add(
            log_file,
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra} | {message}",
            enqueue=True,
            rotation="10 MB",
            retention="30 days"
        )
        _added_handlers.add(log_file)

    # Bind explicit context for every log line to avoid missing-key errors.
    context_clip_id = clip_id if clip_id is not None else "n/a"
    job_logger = logger.bind(job_id=job_id, clip_id=context_clip_id)

    return job_logger

def log_job_event(job_id: int, message: str, level: str = "INFO", clip_id: Optional[int] = None):
    """
    Log an event for a specific job.

    Args:
        job_id: The job ID
        message: The log message
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        clip_id: Optional clip ID
    """
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