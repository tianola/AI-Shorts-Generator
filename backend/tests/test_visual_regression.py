"""Tests for services/visual_regression.py: frame extraction, SSIM/PSNR comparison, the
per-frame video comparison, and the approve-then-check baseline workflow.

The fixtures are tiny ffmpeg-generated images and clips built once per module. Nothing touches
the network. Tests that need ffmpeg are skipped with a reason when it is missing.

Run from backend/:  python -m pytest tests/test_visual_regression.py -q
"""
import json
import os
import shutil
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import pytest

from services import visual_regression as vr

TIMEOUT = 120
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg and ffprobe on PATH")


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=TIMEOUT,
                   capture_output=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("requires ffmpeg and ffprobe on PATH")
    d = tmp_path_factory.mktemp("visual_regression")
    paths = {}

    # 540x960 test pattern, one frame.
    paths["pattern"] = str(d / "pattern.png")
    _ff("-f", "lavfi", "-i", "testsrc2=size=540x960", "-frames:v", "1", paths["pattern"])

    # Same pattern with a small brightness lift (eq brightness is in -1..1).
    paths["bright"] = str(d / "bright.png")
    _ff("-f", "lavfi", "-i", "testsrc2=size=540x960", "-vf", "eq=brightness=0.05",
        "-frames:v", "1", paths["bright"])

    # Heavy noise on a flat grey frame: clearly different structure.
    paths["noise"] = str(d / "noise.png")
    _ff("-f", "lavfi", "-i", "color=c=gray:s=540x960", "-vf", "noise=alls=100:allf=t",
        "-frames:v", "1", paths["noise"])

    # Three-second 540x960 clips: the test pattern, and a solid blue clip.
    paths["video"] = str(d / "video.mp4")
    _ff("-f", "lavfi", "-i", "testsrc2=size=540x960:rate=15:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", paths["video"])
    paths["other_video"] = str(d / "other.mp4")
    _ff("-f", "lavfi", "-i", "color=c=blue:s=540x960:rate=15:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", paths["other_video"])
    return paths


# ---------------------------------------------------------------------------
# compare_images
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_identical_images_score_one_and_hundred_db(media):
    result = vr.compare_images(media["pattern"], media["pattern"])
    assert result == {"ssim": 1.0, "psnr": 100.0}


@needs_ffmpeg
def test_clearly_different_images_fall_below_ssim_threshold(media):
    result = vr.compare_images(media["pattern"], media["noise"])
    assert result is not None
    assert result["ssim"] < 0.9, result


@needs_ffmpeg
def test_small_brightness_shift_keeps_ssim_high_but_lowers_psnr(media):
    identical = vr.compare_images(media["pattern"], media["pattern"])
    shifted = vr.compare_images(media["pattern"], media["bright"])
    assert shifted is not None
    assert shifted["ssim"] > 0.95, shifted
    assert shifted["psnr"] < identical["psnr"], (shifted, identical)


def test_missing_image_returns_none(tmp_path, media):
    assert vr.compare_images(str(tmp_path / "absent.png"), media["pattern"]) is None


# ---------------------------------------------------------------------------
# extract_frame
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_extract_frame_writes_non_empty_png(media, tmp_path):
    out = tmp_path / "nested" / "frame.png"
    assert vr.extract_frame(media["video"], 1.0, str(out)) is True
    assert out.is_file() and out.stat().st_size > 0


def test_extract_frame_missing_video_is_false(tmp_path):
    out = tmp_path / "frame.png"
    assert vr.extract_frame(str(tmp_path / "no_such.mp4"), 1.0, str(out)) is False
    assert not out.exists()


@needs_ffmpeg
def test_extract_frame_beyond_end_does_not_raise(media, tmp_path):
    # The result is either a clean False or a frame; it must never raise.
    result = vr.extract_frame(media["video"], 999.0, str(tmp_path / "late.png"))
    assert result in (True, False)


# ---------------------------------------------------------------------------
# compare_video_frames
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_compare_video_frames_one_row_per_pair(media, tmp_path):
    # Same video on both sides, same moments: every pair is an identical frame.
    pairs = [(0.5, 0.5), (1.0, 1.0)]
    rows = vr.compare_video_frames(media["video"], media["video"], pairs, str(tmp_path / "work"))
    assert len(rows) == 2
    assert rows[0]["time_a"] == 0.5 and rows[0]["time_b"] == 0.5
    assert rows[1]["time_a"] == 1.0 and rows[1]["time_b"] == 1.0
    for row in rows:
        assert row["ssim"] == pytest.approx(1.0, abs=1e-3)
        assert row["psnr"] is not None

    # Different moments on each side are compared as given (the times are reported as they are).
    shifted = vr.compare_video_frames(media["video"], media["video"], [(0.5, 1.5)], str(tmp_path / "work2"))
    assert len(shifted) == 1
    assert shifted[0]["time_a"] == 0.5 and shifted[0]["time_b"] == 1.5
    assert shifted[0]["ssim"] is not None and shifted[0]["ssim"] < 1.0


def test_compare_video_frames_none_metrics_when_frame_cannot_be_cut(tmp_path):
    rows = vr.compare_video_frames(str(tmp_path / "missing_a.mp4"), str(tmp_path / "missing_b.mp4"),
                                   [(1.0, 1.0)], str(tmp_path / "work"))
    assert rows == [{"time_a": 1.0, "time_b": 1.0, "ssim": None, "psnr": None}]


# ---------------------------------------------------------------------------
# capture_baseline
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_capture_baseline_writes_frames_and_record(media, tmp_path):
    out_dir = tmp_path / "baseline"
    record = vr.capture_baseline(media["video"], [0.5, 1.5], str(out_dir), label="intro")

    assert record["label"] == "intro"
    assert [f["file"] for f in record["frames"]] == ["frame_0.png", "frame_1.png"]
    assert [f["time"] for f in record["frames"]] == [0.5, 1.5]
    for frame in record["frames"]:
        assert (out_dir / frame["file"]).stat().st_size > 0

    on_disk = json.loads((out_dir / vr.BASELINE_FILE).read_text(encoding="utf-8"))
    assert on_disk == record


def test_capture_baseline_raises_when_frame_cannot_be_cut(tmp_path):
    with pytest.raises(RuntimeError):
        vr.capture_baseline(str(tmp_path / "missing.mp4"), [1.0], str(tmp_path / "baseline"))


# ---------------------------------------------------------------------------
# check_baseline
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_check_baseline_passes_for_the_same_video(media, tmp_path):
    base = tmp_path / "baseline"
    vr.capture_baseline(media["video"], [0.5, 1.5], str(base))
    result = vr.check_baseline(media["video"], str(base), str(tmp_path / "work"))
    assert result["passed"] is True
    assert len(result["results"]) == 2
    assert all(r["passed"] for r in result["results"])


@needs_ffmpeg
def test_check_baseline_fails_for_a_different_video(media, tmp_path):
    base = tmp_path / "baseline"
    vr.capture_baseline(media["video"], [0.5, 1.5], str(base))
    result = vr.check_baseline(media["other_video"], str(base), str(tmp_path / "work"))
    assert result["passed"] is False
    assert all(r["passed"] is False for r in result["results"])


def test_check_baseline_empty_frames_is_not_a_pass(tmp_path):
    base = tmp_path / "baseline"
    base.mkdir()
    (base / vr.BASELINE_FILE).write_text(json.dumps({"label": "", "frames": []}), encoding="utf-8")
    result = vr.check_baseline("unused.mp4", str(base), str(tmp_path / "work"))
    assert result["passed"] is False
    assert result["results"] == []


@needs_ffmpeg
def test_check_baseline_respects_custom_thresholds(media, tmp_path):
    base = tmp_path / "baseline"
    vr.capture_baseline(media["video"], [0.5], str(base))

    # SSIM can never exceed 1.0, so a tolerance above it must fail even an identical render.
    strict = vr.check_baseline(media["video"], str(base), str(tmp_path / "w1"), ssim_min=1.01)
    assert strict["passed"] is False
    assert strict["ssim_min"] == 1.01
    assert strict["results"][0]["ssim"] == pytest.approx(1.0, abs=1e-3)

    # The default tolerances are recorded and an identical render passes them.
    default = vr.check_baseline(media["video"], str(base), str(tmp_path / "w2"))
    assert default["ssim_min"] == vr.SSIM_MIN and default["psnr_min"] == vr.PSNR_MIN
    assert default["passed"] is True
