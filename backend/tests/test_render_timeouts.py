"""Defect 4 — ffmpeg/ffprobe calls with no timeout.

``subprocess.run`` without ``timeout=`` waits forever. A decoder wedged on a
damaged stream therefore held the whole render job open indefinitely, with no
error line in the log — the failure mode this repo has already been burned by
twice (the "frozen worker" class of bug). Every call in the render chain has to
be bounded, and a bounded call has to fail the way a failure looks here: an
``Exception`` the caller already handles, and no half-written deliverable.
"""
import ast
import os
import shutil
import subprocess
import time

import pytest

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = [
    os.path.join(BACKEND, "services", "render.py"),
    os.path.join(BACKEND, "services", "sfx.py"),
    os.path.join(BACKEND, "services", "audio_polish.py"),
    os.path.join(BACKEND, "services", "smart_crop.py"),
    os.path.join(BACKEND, "services", "anti_copyright.py"),
    os.path.join(BACKEND, "services", "quality", "technical.py"),
]


def _subprocess_calls(path):
    """Every subprocess.run/check_output/Popen call site in a file, with its line."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
                and func.attr in ("run", "check_output", "check_call", "Popen")):
            keywords = {k.arg for k in node.keywords}
            found.append((node.lineno, func.attr, keywords))
    return found


@pytest.mark.parametrize("path", MODULES, ids=lambda p: os.path.basename(p))
def test_no_subprocess_call_is_unbounded(path):
    """The defect class, not one instance of it.

    A new unbounded call in any of these modules fails here — which is the only
    reason the fix survives the next person to touch the render chain.
    """
    unbounded = [(line, attr) for line, attr, kw in _subprocess_calls(path)
                 if "timeout" not in kw]
    assert not unbounded, (
        f"{os.path.basename(path)} has subprocess calls with no timeout "
        f"(a wedged ffmpeg hangs the job forever): {unbounded}")
    assert _subprocess_calls(path), f"{path} has no subprocess calls at all?"


def _make_source(path, duration=5.0):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"testsrc=size=320x180:rate=30:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
        check=True, capture_output=True, timeout=180)
    return path


@needs_ffmpeg
def test_render_single_pass_kills_a_wedged_ffmpeg_instead_of_hanging(tmp_path, monkeypatch):
    """A render that outlives its ceiling must raise, and must leave no
    half-written clip behind for the UI to serve as a finished short."""
    from services import render

    src = _make_source(str(tmp_path / "src.mp4"))
    out = str(tmp_path / "final_video.mp4")
    # Any real encode of this clip outlives 50 ms, so the ceiling is what fires.
    monkeypatch.setattr(render, "ENCODE_TIMEOUT_S", 0.05)

    started = time.monotonic()
    with pytest.raises(Exception) as exc_info:
        render.render_single_pass(src, 0.0, 5.0, out)
    elapsed = time.monotonic() - started

    assert "timed out" in str(exc_info.value).lower(), exc_info.value
    assert elapsed < 60, f"bounded call still took {elapsed:.1f}s to let go"
    assert not os.path.exists(out), "a killed render must not produce the deliverable"
    leftovers = [f for f in os.listdir(str(tmp_path)) if ".part" in f]
    assert not leftovers, f"temp file left behind: {leftovers}"


@needs_ffmpeg
def test_sfx_mix_falls_back_to_the_source_when_ffmpeg_wedges(tmp_path, monkeypatch):
    """sfx never fails a job: it hands back the un-mixed clip. That has to be
    true for a wedged ffmpeg too, not only for a non-zero exit."""
    from services import sfx

    src = _make_source(str(tmp_path / "src.mp4"))
    hit = str(tmp_path / "hit.wav")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=2000:sample_rate=48000:duration=0.2", hit],
                   check=True, capture_output=True, timeout=120)
    monkeypatch.setattr(sfx, "_find_sfx_file", lambda name: hit)
    monkeypatch.setattr(sfx, "ENCODE_TIMEOUT_S", 0.05)

    out = str(tmp_path / "out.mp4")
    started = time.monotonic()
    assert sfx.mix_sfx_into_video(src, out, ["whoosh_1"]) == out
    assert time.monotonic() - started < 60
    assert os.path.getsize(out) == os.path.getsize(src), "expected the copied source"


@needs_ffmpeg
def test_loudness_measure_pass_is_bounded_too(tmp_path, monkeypatch):
    """The measurement pass is new ffmpeg surface: it inherits the same rule."""
    import services.audio_polish as audio_polish

    src = _make_source(str(tmp_path / "src.mp4"))
    monkeypatch.setattr(audio_polish, "ANALYSIS_TIMEOUT_S", 0.001)
    started = time.monotonic()
    assert audio_polish.measure_loudness(src) is None
    assert time.monotonic() - started < 60
