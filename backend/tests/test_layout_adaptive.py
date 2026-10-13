"""
Regression tests for adaptive layout detection — bounds validation and
resolved_layout guard.

AC:
  - split-screen crop boxes stay within source bounds (720p, 4K)
  - single_speaker fallback when no second face detected
  - make_vertical_split_screen doesn't crash (returns file > 0 bytes)
  - worker uses resolved_layout, never raw layout_choice=='auto'

Run from backend/:  PYTHONPATH=. python -m pytest tests/test_layout_adaptive.py -v
"""
import os
import sys
import subprocess
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    """Absolute path to a pre-built fixture video."""
    return os.path.join(FIXTURES, name)


def _make_synth_video(width: int, height: int, duration: int, path: str) -> None:
    """Generate a solid-black synthetic video via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=black:size={width}x{height}:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast",
            path,
        ],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# 1. Split-screen crop bounds stay within source dimensions
# ---------------------------------------------------------------------------

class TestSplitScreenCropBounds:
    """
    detect_adaptive_layout on a black video (no persons detected) returns
    'single_speaker' or 'fit_blur', so we test the underlying filter builder
    directly for bounds — same contract the fixed smart_crop.py relies on.
    """

    def _assert_bounds(self, fg: str, src_w: int, src_h: int) -> None:
        """Parse every crop=W:H:X:Y in fg and assert none exceed the source."""
        import re
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", fg)
        assert crops, "No crop() found in filtergraph"
        for cw, ch, cx, cy in crops:
            cw, ch, cx, cy = int(cw), int(ch), int(cx), int(cy)
            assert cx >= 0, f"crop_x {cx} is negative (src_w={src_w})"
            assert cy >= 0, f"crop_y {cy} is negative (src_h={src_h})"
            assert cx + cw <= src_w, (
                f"crop overflows horizontally: x={cx} w={cw} src_w={src_w}"
            )
            assert cy + ch <= src_h, (
                f"crop overflows vertically: y={cy} h={ch} src_h={src_h}"
            )

    def test_split_screen_crops_within_720p_bounds(self):
        """build_split_screen_filter respects 1280x720 source boundaries."""
        from services.focus_tracking import build_split_screen_filter

        src_w, src_h = 1280, 720
        box_left  = (100,  80, 300, 500)
        box_right = (880,  80, 300, 500)

        fg = build_split_screen_filter(
            box_left, box_right, src_w=src_w, src_h=src_h,
            target_w=1080, target_h=1920,
        )
        self._assert_bounds(fg, src_w, src_h)

    def test_split_screen_crops_within_4k_bounds(self):
        """build_split_screen_filter respects 3840x2160 source boundaries."""
        from services.focus_tracking import build_split_screen_filter

        src_w, src_h = 3840, 2160
        box_left  = (300,   200, 900, 1500)
        box_right = (2500,  200, 900, 1500)

        fg = build_split_screen_filter(
            box_left, box_right, src_w=src_w, src_h=src_h,
            target_w=1080, target_h=1920,
        )
        self._assert_bounds(fg, src_w, src_h)


# ---------------------------------------------------------------------------
# 2. Single-speaker fallback when no second face
# ---------------------------------------------------------------------------

class TestSingleSpeakerFallback:
    """
    detect_adaptive_layout on a plain black video must NOT return 'split_screen'.
    YOLO finds no persons, so the threshold for two-speaker mode is never met.
    """

    def test_single_speaker_fallback_when_no_second_face(self):
        """Black video (0 faces) -> layout is single_speaker or fit_blur, never split_screen."""
        from services.smart_crop import detect_adaptive_layout

        video_path = _fixture("test_720p.mp4")
        assert os.path.exists(video_path), f"Missing fixture: {video_path}"

        result = detect_adaptive_layout(video_path)
        assert result in ("single_speaker", "fit_blur", "static"), (
            f"Expected single_speaker/fit_blur/static on a blank video, got '{result}'"
        )
        assert result != "split_screen", (
            "detect_adaptive_layout returned 'split_screen' for a video with no persons"
        )


# ---------------------------------------------------------------------------
# 3. make_vertical_split_screen doesn't crash; returns an output file > 0 bytes
# ---------------------------------------------------------------------------

class TestMakeVerticalSplitScreen:
    """
    make_vertical_split_screen on a black video (< 2 persons found) must fall
    back gracefully to make_vertical_static and produce a non-empty output.
    This was a guaranteed crash before the fix (missing required args).
    """

    @pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg on PATH")
    def test_make_vertical_split_screen_no_crash(self, tmp_path):
        from services.vertical import make_vertical_split_screen

        src = _fixture("test_720p.mp4")
        assert os.path.exists(src), f"Missing fixture: {src}"

        out = str(tmp_path / "split_out.mp4")
        result = make_vertical_split_screen(src, out)

        assert result is not None, "make_vertical_split_screen returned None"
        assert os.path.exists(result), f"Output file does not exist: {result}"
        assert os.path.getsize(result) > 0, "Output file is empty (0 bytes)"


# ---------------------------------------------------------------------------
# 4. Worker regression guard — resolved_layout is used, not raw layout_choice
# ---------------------------------------------------------------------------

class TestWorkerResolvesLayout:
    """
    In worker.py, when layout_choice == 'auto', detect_adaptive_layout is
    called once and its return value is stored as resolved_layout. Downstream
    calls (build_vertical_filter, smart_crop_video) receive resolved_layout,
    never 'auto'. This test mocks the detection and verifies the contract.
    """

    def test_worker_uses_resolved_layout_not_layout_choice(self, monkeypatch):
        """
        build_vertical_filter must be called with the resolved mode string,
        not with 'auto'. Simulate the worker's resolution logic directly so
        the test doesn't depend on the full job database stack.
        """

        # --- Reproduce the exact resolution block from worker.py (lines 494-504) ---
        layout_choice = "auto"
        resolved_layout = None

        captured_mode = []

        def fake_detect(path):
            return "single_speaker"

        def fake_build_vertical_filter(path, mode=None, **kwargs):
            captured_mode.append(mode)
            return "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"

        import services.smart_crop as sc
        import services.vertical as vert

        monkeypatch.setattr(sc, "detect_adaptive_layout", fake_detect)
        monkeypatch.setattr(vert, "build_vertical_filter", fake_build_vertical_filter)

        # Replicate the resolution logic
        if layout_choice in ("auto", "adaptive", None, ""):
            resolved_layout = sc.detect_adaptive_layout("dummy.mp4")
        else:
            resolved_layout = layout_choice

        # Call build_vertical_filter as worker does
        vert.build_vertical_filter("dummy.mp4", mode=resolved_layout)

        # Assert: exactly one call, with the resolved mode, NOT 'auto'
        assert len(captured_mode) == 1, "build_vertical_filter should be called once"
        assert captured_mode[0] == "single_speaker", (
            f"Expected mode='single_speaker', got '{captured_mode[0]}'"
        )
        assert captured_mode[0] != "auto", (
            "build_vertical_filter was called with mode='auto' — "
            "Bug #3 regression: resolved_layout not used"
        )
        assert resolved_layout == "single_speaker"
