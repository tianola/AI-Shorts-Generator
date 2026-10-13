"""Shared test configuration.

Three hazards this file exists to close:

* The database. Only one module used to set TEST_DATABASE_URL, and whichever
  module imported database.session first decided the engine for the whole
  process - so a bare `pytest` run could write job rows into the real
  storage/app.db. It is forced here, before any test module is imported.
* The storage tree. Routes and the worker resolve settings.JOBS_DIR at call
  time, so a test posting a fake upload wrote source.mp4 into
  storage/jobs/<id>/ where <id> can belong to a real job.
* The application log. utils/logger.py adds its rotating file sink at import
  time, so a test that deliberately provokes an error writes it into
  storage/logs/app.log - the same file health_check.py reads to decide whether
  the install is healthy. APP_LOG_PATH moves it into the system temp directory.

Tests that shell out to ffmpeg/ffprobe are skipped with an explicit reason when
the binaries are missing, instead of failing with a bare FileNotFoundError that
hides real regressions: whole modules listed below, single tests via the
FFMPEG_REQUIRED marker.
"""
import atexit
import os
import shutil
import tempfile

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

_LOG_DIR = tempfile.mkdtemp(prefix="ai-shorts-test-logs-")
os.environ.setdefault("APP_LOG_PATH", os.path.join(_LOG_DIR, "app.log"))
atexit.register(shutil.rmtree, _LOG_DIR, ignore_errors=True)

import pytest

FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

FFMPEG_REQUIRED = pytest.mark.skipif(
    not FFMPEG_AVAILABLE, reason="requires ffmpeg and ffprobe on PATH"
)

# Modules where every test builds or probes real media.
_MEDIA_MODULES = {"test_precision_trim.py"}


def pytest_collection_modifyitems(config, items):
    if FFMPEG_AVAILABLE:
        return
    for item in items:
        if item.path.name in _MEDIA_MODULES:
            item.add_marker(pytest.mark.skip(reason="requires ffmpeg and ffprobe on PATH"))


@pytest.fixture(scope="session", autouse=True)
def _job_files_stay_out_of_storage(tmp_path_factory):
    """Point settings.JOBS_DIR at a temp directory for the whole session."""
    from config import settings

    jobs_dir = tmp_path_factory.mktemp("jobs")
    original = type(settings).JOBS_DIR
    type(settings).JOBS_DIR = property(lambda self: str(jobs_dir))
    try:
        yield
    finally:
        type(settings).JOBS_DIR = original
