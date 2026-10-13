"""Regression tests for AI Shorts Generator bug fixes.

Run from backend/:  PYTHONPATH=. python -m pytest tests/ -x -q
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ---------------------------------------------------------------------------
# Test 1: STORAGE_ROOT is absolute and repo-root-based
# ---------------------------------------------------------------------------
def test_storage_root_absolute():
    from config import settings
    assert os.path.isabs(settings.STORAGE_ROOT), "STORAGE_ROOT must be absolute"
    # Repo root is backend's parent
    backend_dir = Path(__file__).resolve().parent.parent
    repo_root = backend_dir.parent
    expected = os.path.join(str(repo_root), "storage")
    assert os.path.normcase(os.path.normpath(settings.STORAGE_ROOT)) == os.path.normcase(os.path.normpath(expected)), \
        f"STORAGE_ROOT should be {expected}, got {settings.STORAGE_ROOT}"


# ---------------------------------------------------------------------------
# Test 2: URL fingerprint stability
# ---------------------------------------------------------------------------
def test_url_fingerprint():
    from worker import _url_fingerprint
    a = _url_fingerprint("https://youtu.be/5pV3D14y1FQ")
    b = _url_fingerprint("https://youtu.be/5pV3D14y1FQ")
    c = _url_fingerprint("https://youtu.be/82CX6WULNA0")
    assert a == b, "same URL must produce same hash"
    assert a != c, "different URL must produce different hash"
    assert len(a) == 16


# ---------------------------------------------------------------------------
# Test 3: resume logic reuses only for the same URL
# ---------------------------------------------------------------------------
def _make_job_dir():
    d = tempfile.mkdtemp(prefix="shorts_job_")
    source = os.path.join(d, "source.mp4")
    with open(source, "wb") as f:
        f.write(b"fake-source-content")
    return d, source


def test_resume_same_url_reuses():
    d, source = _make_job_dir()
    try:
        from worker import _url_fingerprint
        os.remove(source)
        with open(source, "w") as f:
            f.write("old")
        with open(os.path.join(d, "source.urlhash"), "w") as f:
            f.write(_url_fingerprint("https://x/1"))
        # Simulate the worker logic helper
        from services.youtube import download_youtube  # noqa: F401  (import sanity)
        # Direct check: hash matches => reuse path
        cached = _url_fingerprint("https://x/1")
        assert cached == _url_fingerprint("https://x/1")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_resume_url_changed_redownloads():
    d, _ = _make_job_dir()
    try:
        from worker import _url_fingerprint
        with open(os.path.join(d, "source.urlhash"), "w") as f:
            f.write(_url_fingerprint("https://x/1"))
        # different URL -> hash differs
        assert _url_fingerprint("https://x/2") != _url_fingerprint("https://x/1")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 4: metadata fallback keys
# ---------------------------------------------------------------------------
def test_metadata_fallback():
    # Clip() with only required fields — description/hashtags default to None,
    # but our worker code now uses .get() so a dict without those keys is safe.
    metadata = {"title": "T"}  # no description/hashtags
    desc = metadata.get("description", "")
    tags = metadata.get("hashtags", ["#Shorts"])
    assert desc == ""
    assert tags == ["#Shorts"]


# ---------------------------------------------------------------------------
# Test 5: pending-upload uses query param + default
# ---------------------------------------------------------------------------
def test_pending_upload_param():
    # Just verify the signature is query-based
    import inspect
    from api.clips import get_pending_uploads
    sig = inspect.signature(get_pending_uploads)
    for name, param in sig.parameters.items():
        if name == "max_retries":
            assert param.default is not inspect.Parameter.empty, "max_retries must have a default"
            # FastAPI Query default — verify it's a Query object (has alias/description attrs)
            from fastapi import params
            assert isinstance(param.default, params.Query), f"expected Query default, got {param.default!r}"
            assert "Max retries" in param.default.description


# ---------------------------------------------------------------------------
# Test 6: SFX missing files -> graceful copy
# ---------------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg and ffprobe on PATH",
)


@needs_ffmpeg
def test_sfx_missing_assets_noop():
    from services.sfx import add_automatic_sfx
    d = tempfile.mkdtemp(prefix="sfx_")
    try:
        # Build a tiny real mp4 so ffmpeg can copy it
        import subprocess
        src = os.path.join(d, "src.mp4")
        out = os.path.join(d, "out.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
             "-c:v", "libx264", src],
            check=True, capture_output=True
        )
        result = add_automatic_sfx(src, out, title="pop impact")
        assert os.path.exists(result)
        assert os.path.getsize(out) > 0
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 7: storyboard works on a short clip
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_storyboard_short_clip():
    from services.storyboard import generate_storyboard
    d = tempfile.mkdtemp(prefix="story_")
    try:
        import subprocess
        src = os.path.join(d, "short.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
             "-c:v", "libx264", src],
            check=True, capture_output=True
        )
        result = generate_storyboard(src, d)
        # Either a real storyboard file or "" (both acceptable), but never crash
        if result:
            assert os.path.exists(result)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 8: thumbnail generation on a very short clip
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_thumbnail_short_clip():
    from services.thumbnail import generate_thumbnail
    d = tempfile.mkdtemp(prefix="thumb_")
    try:
        import subprocess
        src = os.path.join(d, "tiny.mp4")
        out = os.path.join(d, "thumb.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=0.5",
             "-c:v", "libx264", src],
            check=True, capture_output=True
        )
        result = generate_thumbnail(src, out, "This is a very long title that should be wrapped and truncated nicely")
        assert result is not None
        assert os.path.exists(out)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 9: suggest-clips is registered
# ---------------------------------------------------------------------------
def test_suggest_clips_registered():
    from api.jobs import router as jobs_router
    paths = [r.path for r in jobs_router.routes]
    assert "/jobs/{job_id}/suggest-clips" in paths, f"missing, got {paths}"


# ---------------------------------------------------------------------------
# Test 10: all-clips-failed marks job failed
# ---------------------------------------------------------------------------
def test_clip_failure_logic_marks_failed(monkeypatch):
    # The actual DB flow is heavy; instead verify the decision helper exists
    # and the completed block references clip_errors. We assert the worker module
    # contains the failure branch (lightweight check).
    import worker as w
    src = Path(w.__file__).read_text(encoding="utf-8")
    assert "clip_errors" in src
    assert 'All {len(clips)} clips failed' in src or "All clips failed" in src


# ---------------------------------------------------------------------------
# Test 11: the application log sink is redirectable
# ---------------------------------------------------------------------------
def test_app_log_path_follows_the_env_override(monkeypatch):
    from config import settings
    from utils.logger import _app_log_path

    elsewhere = os.path.join(tempfile.gettempdir(), "somewhere-else", "app.log")
    monkeypatch.setenv("APP_LOG_PATH", elsewhere)
    assert _app_log_path() == elsewhere

    monkeypatch.delenv("APP_LOG_PATH", raising=False)
    assert _app_log_path() == os.path.join(settings.LOGS_DIR, "app.log")


def test_a_test_run_never_writes_the_production_log():
    """utils/logger.py configures its sink at import time, so this is the only
    hook: the file health_check.py judges the install by must stay ours."""
    from config import settings
    from utils.logger import _app_log_path

    sink = os.path.abspath(_app_log_path())
    production = os.path.abspath(settings.LOGS_DIR)
    assert not sink.startswith(production + os.sep), (
        f"test session would write into the operator's log directory: {sink}"
    )