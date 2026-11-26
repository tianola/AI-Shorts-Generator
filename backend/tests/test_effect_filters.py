"""Tests for the timeline visual effects (services/editing/effect_filters.py) and their wiring into
the single-pass renderer.

Run from backend/:  python -m pytest tests/test_effect_filters.py -q
"""
import json
import os
import re
import shutil
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests"))

import pytest

from services.editing import effect_filters as ef
from test_filtergraphs import _assert_valid

TIMEOUT = 300
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

ZOOM = [{"kind": "zoom_punch", "start": 0.5, "end": 1.5, "intensity": 0.6}]
SHAKE = [{"kind": "shake", "start": 0.5, "end": 2.5, "intensity": 1.0}]
FLASH = [{"kind": "flash", "start": 1.0, "end": 1.2, "intensity": 1.0}]
TINT = [{"kind": "tint_red", "start": 0.0, "end": 2.0, "intensity": 1.0}]
ALL_FOUR = ZOOM + SHAKE + FLASH + TINT


def _probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
         "-of", "json", path],
        capture_output=True, text=True, check=True, timeout=TIMEOUT,
    )
    return json.loads(r.stdout)


def _video_stream(info):
    return next(s for s in info["streams"] if s.get("codec_type") == "video")


# ---------------------------------------------------------------------------
# 1. Empty input
# ---------------------------------------------------------------------------
def test_no_tracks_returns_none():
    assert ef.build_effects_fragment(None) is None
    assert ef.build_effects_fragment([]) is None


def test_unknown_kinds_only_returns_none():
    assert ef.build_effects_fragment([{"kind": "wobble", "start": 0, "end": 1, "intensity": 1}]) is None


# ---------------------------------------------------------------------------
# 2. Zoom only
# ---------------------------------------------------------------------------
def test_zoom_only_fragment_shape_and_determinism():
    frag = ef.build_effects_fragment(ZOOM)
    assert frag.startswith("[0:v]split=2[fxa][fxb]")
    assert "scale=w=" in frag
    assert ":eval=frame" in frag
    assert "overlay=" in frag
    assert frag.count(":eval=frame") >= 2
    assert frag.endswith("[vfx]")
    assert "random(" not in frag
    assert ef.build_effects_fragment(ZOOM) == frag


# ---------------------------------------------------------------------------
# 3. Shake only
# ---------------------------------------------------------------------------
def test_shake_only_uses_both_axes():
    frag = ef.build_effects_fragment(SHAKE)
    assert "sin(2*PI*17*t)" in frag
    assert "cos(2*PI*23*t)" in frag
    assert frag.endswith("[vfx]")


def test_shake_travel_stays_inside_headroom():
    # The enlarged copy extends headroom*W/2 past each edge; the travel must stay inside that.
    assert ef.SHAKE_TRAVEL_SHARE < 1
    for strength in (0.0, 1.0):
        headroom = ef.SHAKE_HEADROOM_BASE + ef.SHAKE_HEADROOM_RANGE * strength
        for size in (1080, 1920):
            travel = headroom * size / 2 * ef.SHAKE_TRAVEL_SHARE
            assert travel < headroom * size / 2, (strength, size, travel)
    # The coefficient written into the fragment is the travel computed above (intensity 1.0).
    headroom = ef.SHAKE_HEADROOM_BASE + ef.SHAKE_HEADROOM_RANGE
    assert f"{ef._num(headroom * 1080 / 2 * ef.SHAKE_TRAVEL_SHARE)}*" in ef.build_effects_fragment(SHAKE)


# ---------------------------------------------------------------------------
# 4. Flash and tint
# ---------------------------------------------------------------------------
def test_flash_only():
    frag = ef.build_effects_fragment(FLASH)
    assert frag.startswith("[0:v]eq=brightness=")
    assert "enable='between(t,1,1.2)'" in frag
    assert frag.endswith("[vfx]")


def test_two_flashes_join_enable_with_plus():
    frag = ef.build_effects_fragment([
        {"kind": "flash", "start": 1.0, "end": 1.2, "intensity": 1.0},
        {"kind": "flash", "start": 3.0, "end": 3.2, "intensity": 0.5},
    ])
    assert "enable='between(t,1,1.2)+between(t,3,3.2)'" in frag


def test_tint_only_is_red_shift():
    frag = ef.build_effects_fragment(TINT)
    assert "colorchannelmixer=" in frag
    rr = float(re.search(r"rr=([-0-9.]+)", frag).group(1))
    gg = float(re.search(r"gg=([-0-9.]+)", frag).group(1))
    bb = float(re.search(r"bb=([-0-9.]+)", frag).group(1))
    assert rr > 1
    assert gg < 1
    assert bb < 1
    assert "enable='between(t," in frag
    assert frag.endswith("[vfx]")


# ---------------------------------------------------------------------------
# 5. All four together
# ---------------------------------------------------------------------------
def test_all_four_single_balanced_fragment():
    frag = ef.build_effects_fragment(ALL_FOUR)
    assert isinstance(frag, str)
    assert frag.count("(") == frag.count(")")
    assert frag.count("'") % 2 == 0
    assert frag.endswith("[vfx]")
    assert frag.count("[vfx]") == 1
    assert "eq=brightness=" in frag and "colorchannelmixer=" in frag and "overlay=" in frag


# ---------------------------------------------------------------------------
# 6. Real ffmpeg accepts the fragment
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_all_four_fragment_runs_in_ffmpeg(tmp_path):
    src = str(tmp_path / "in.mp4")
    out = str(tmp_path / "out.mp4")
    # 15 fps so that 60 frames is 4 s.
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=15:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True, timeout=TIMEOUT,
    )
    frag = ef.build_effects_fragment(ALL_FOUR)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-filter_complex", frag, "-map", "[vfx]", "-frames:v", "60", out],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    info = _probe(out)
    v = _video_stream(info)
    assert (v["width"], v["height"]) == (1080, 1920)
    assert 3.5 <= float(info["format"]["duration"]) <= 4.5


# ---------------------------------------------------------------------------
# 7. No black edge during a shake (the key safety test)
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_no_black_edges_during_shake(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    src = str(tmp_path / "white.mp4")
    out = str(tmp_path / "shaken.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=white:s=1080x1920:d=3:r=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True, timeout=TIMEOUT,
    )
    tracks = [
        {"kind": "shake", "start": 0.5, "end": 2.5, "intensity": 1.0},
        {"kind": "zoom_punch", "start": 0.0, "end": 0.7, "intensity": 1.0},
    ]
    frag = ef.build_effects_fragment(tracks)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-filter_complex", frag, "-map", "[vfx]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert r.returncode == 0, r.stderr[-2000:]

    for t in (0.2, 0.6, 1.0, 1.5, 2.0):
        png = str(tmp_path / f"frame_{t}.png")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", out, "-frames:v", "1", png],
            check=True, capture_output=True, timeout=TIMEOUT,
        )
        gray = Image.open(png).convert("L")
        lowest = gray.getextrema()[0]
        assert lowest > 240, f"frame at {t}s has a dark pixel (min luma {lowest}); black edge?"


# ---------------------------------------------------------------------------
# 8. Wiring into build_single_pass_filter
# ---------------------------------------------------------------------------
def test_effects_stage_sits_before_captions():
    from services.render import build_single_pass_filter

    zoom = ef.build_effects_fragment(ZOOM)
    _, fc, vmap, _ = build_single_pass_filter(
        "[0:v]scale=1080:1920[vsingle]",
        captions_path="x.ass",
        effects_frag=zoom,
        has_audio=True,
    )
    assert fc.index("[vfx]") < fc.index("subtitles="), fc
    assert vmap == "[vout]"
    _assert_valid(fc, "single pass with zoom effects and captions")


# ---------------------------------------------------------------------------
# 9. render_single_pass end to end
# ---------------------------------------------------------------------------
@pytest.fixture
def tiny_clip(tmp_path):
    src = str(tmp_path / "src.mp4")
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", "color=c=purple:s=320x180:d=1.5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
         "-shortest", "-c:v", "libx264", "-c:a", "aac", src],
        check=True, capture_output=True, timeout=TIMEOUT,
    )
    return src


def _spy_on_ffmpeg(monkeypatch):
    from services import render

    captured = []
    real = render._run_ffmpeg_and_verify

    def spy(cmd, output_path, label, min_duration=0.0):
        captured.append(cmd)
        return real(cmd, output_path, label, min_duration=min_duration)

    monkeypatch.setattr(render, "_run_ffmpeg_and_verify", spy)
    return captured


def _filter_complex(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


ZOOM_SHORT = [{"kind": "zoom_punch", "start": 0.2, "end": 1.0, "intensity": 0.8}]


@needs_ffmpeg
def test_render_single_pass_applies_zoom(tiny_clip, tmp_path, monkeypatch):
    from config import settings
    from services import render

    monkeypatch.setattr(settings, "EFFECTS_ENABLED", True)
    monkeypatch.setattr(settings, "EFFECTS_DISABLED_KINDS", "")
    captured = _spy_on_ffmpeg(monkeypatch)

    out = str(tmp_path / "clip.mp4")
    result = render.render_single_pass(tiny_clip, 0.0, 1.5, out, effects=ZOOM_SHORT)
    assert os.path.exists(result)
    assert "fxa" in _filter_complex(captured[-1])

    info = _probe(result)
    v = _video_stream(info)
    assert (v["width"], v["height"]) == (1080, 1920)
    assert abs(float(info["format"]["duration"]) - 1.5) <= 0.3


@needs_ffmpeg
def test_render_single_pass_effects_disabled_by_setting(tiny_clip, tmp_path, monkeypatch):
    from config import settings
    from services import render

    monkeypatch.setattr(settings, "EFFECTS_ENABLED", False)
    captured = _spy_on_ffmpeg(monkeypatch)

    out = str(tmp_path / "plain.mp4")
    result = render.render_single_pass(tiny_clip, 0.0, 1.5, out, effects=ZOOM_SHORT)
    assert os.path.exists(result)
    assert "fxa" not in _filter_complex(captured[-1])


@needs_ffmpeg
def test_render_single_pass_disabled_kinds_renders_plain(tiny_clip, tmp_path, monkeypatch):
    from config import settings
    from services import render

    monkeypatch.setattr(settings, "EFFECTS_ENABLED", True)
    monkeypatch.setattr(settings, "EFFECTS_DISABLED_KINDS", "zoom_punch,shake")
    captured = _spy_on_ffmpeg(monkeypatch)

    out = str(tmp_path / "plain2.mp4")
    result = render.render_single_pass(tiny_clip, 0.0, 1.5, out, effects=ZOOM_SHORT)
    assert os.path.exists(result)
    assert "fxa" not in _filter_complex(captured[-1])
