"""Tests for the single-pass unified render pipeline (Task D).

Run from backend/:  PYTHONPATH=. python -m pytest tests/test_single_pass_unified.py -v
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ---------------------------------------------------------------------------
# _build_timed_crop: converts a YOLO/face trajectory into an ffmpeg crop filter
# with piecewise-linear x/y expressions (smooth follow, no jitter).
# ---------------------------------------------------------------------------
def test_build_timed_crop_returns_crop_with_piecewise_expr():
    from services.vertical import _build_timed_crop

    traj = [
        (0.0, 100, 50, 540, 960),
        (1.0, 200, 60, 540, 960),
        (2.0, 300, 70, 540, 960),
    ]
    result = _build_timed_crop(traj)
    assert result.startswith("crop=540:960")
    assert "x='" in result and "y='" in result
    assert "if(lt(t" in result
    assert "clip((t-" in result  # linear interpolation between keyframes


def test_build_timed_crop_empty_returns_empty():
    from services.vertical import _build_timed_crop

    assert _build_timed_crop([]) == ""


# ---------------------------------------------------------------------------
# build_vertical_filter: produces a filter_complex fragment [0:v] -> [vsingle]
# WITHOUT re-encoding, for every layout mode.
# ---------------------------------------------------------------------------
def test_build_vertical_filter_static():
    from services.vertical import build_vertical_filter

    frag = build_vertical_filter("dummy.mp4", mode="static")
    assert frag.startswith("[0:v]")
    assert "crop=" in frag
    assert "scale=1080:1920" in frag
    assert frag.endswith("[vsingle]")


def test_build_vertical_filter_single_speaker_falls_back_to_static_when_no_faces():
    from services.vertical import build_vertical_filter

    # A path that cannot be opened -> no faces -> static center-crop fallback.
    frag = build_vertical_filter("nonexistent_does_not_exist.mp4", mode="single_speaker")
    assert frag.startswith("[0:v]")
    assert "crop=" in frag
    assert frag.endswith("[vsingle]")


def test_build_vertical_filter_fit_blur():
    from services.vertical import build_vertical_filter

    frag = build_vertical_filter("dummy.mp4", mode="fit_blur")
    assert frag.startswith("[0:v]")
    assert "boxblur" in frag
    assert "overlay" in frag
    assert frag.endswith("[vsingle]")


# ---------------------------------------------------------------------------
# build_single_pass_filter: composes vertical + subtitles + title + broll + sfx
# into ONE filter_complex with [vout]/[aout] maps.
# ---------------------------------------------------------------------------
STATIC_FRAG = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"


def test_single_pass_filter_baseline_vertical_only():
    from services.render import build_single_pass_filter

    extra, fc, vmap, amap = build_single_pass_filter(
        STATIC_FRAG, has_audio=True
    )
    assert extra == []
    assert "[0:v]" in fc and "[vsingle]" in fc
    assert fc.endswith("[vout]")
    assert vmap == "[vout]"
    assert amap == "0:a?"


def test_single_pass_filter_with_subtitles_and_title():
    from services.render import build_single_pass_filter

    extra, fc, vmap, amap = build_single_pass_filter(
        STATIC_FRAG,
        captions_path=r"C:\work\subs.ass",
        title_png=r"C:\work\title.png",
        has_audio=True,
    )
    assert any(i["path"].endswith("title.png") for i in extra)
    assert "subtitles=" in fc
    assert "overlay=" in fc
    assert fc.count("[title]") >= 1
    assert fc.endswith("format=yuv420p[vout]")


def test_single_pass_filter_with_sfx_mixes_audio_without_ducking():
    from services.render import build_single_pass_filter

    extra, fc, vmap, amap = build_single_pass_filter(
        STATIC_FRAG,
        sfx_files=["whoosh_1.wav", "pop_1.wav"],
        sfx_timestamps=[0.3, 2.5],
        has_audio=True,
    )
    assert len(extra) == 2
    assert "[0:a]volume=1.0[orig]" in fc      # voice kept at 100%, never ducked
    assert "adelay=300|300" in fc
    assert "adelay=2500|2500" in fc
    assert "amix=inputs=3:duration=first" in fc
    assert amap == "[aout]"


def test_single_pass_filter_no_audio_stream_skips_audio():
    from services.render import build_single_pass_filter

    extra, fc, vmap, amap = build_single_pass_filter(
        STATIC_FRAG,
        sfx_files=["whoosh_1.wav"],
        sfx_timestamps=[0.3],
        has_audio=False,
    )
    assert amap is None
    assert "amix" not in fc


# ---------------------------------------------------------------------------
# render_single_pass: an actual end-to-end ffmpeg run on a tiny generated clip.
# ---------------------------------------------------------------------------
@pytest.fixture
def tiny_source():
    d = tempfile.mkdtemp(prefix="singlepass_")
    src = os.path.join(d, "src.mp4")
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=purple:s=320x180:d=1.5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
            "-shortest",
            "-c:v", "libx264", "-c:a", "aac",
            src,
        ],
        check=True, capture_output=True,
    )
    yield src, d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg on PATH")
def test_render_single_pass_end_to_end(tiny_source):
    from services.render import render_single_pass
    from services.vertical import build_vertical_filter

    src, d = tiny_source
    out = os.path.join(d, "final_video.mp4")
    frag = build_vertical_filter(src, mode="static")
    result = render_single_pass(src, 0.0, 1.5, out, vertical_frag=frag)
    assert os.path.exists(result)
    assert os.path.getsize(result) > 0

    # verify duration is ~1.5s and it has video+audio
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", result],
        capture_output=True, text=True, check=True,
    )
    import json
    info = json.loads(probe.stdout)
    dur = float(info["format"]["duration"])
    assert 1.2 <= dur <= 2.0
    types = [s.get("codec_type") for s in info.get("streams", [])]
    assert "video" in types
    assert "audio" in types