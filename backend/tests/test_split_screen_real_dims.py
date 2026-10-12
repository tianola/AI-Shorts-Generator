"""
Regression tests for split-screen hardcoded dimension bugs.

AC: Split-screen filter builds correct crops for non-1080p sources;
    crops must stay within source bounds.

Run from backend/:  PYTHONPATH=. python -m pytest tests/test_split_screen_real_dims.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.focus_tracking import build_split_screen_filter


def _parse_crop(filtergraph: str, label: str) -> tuple[int, int, int, int]:
    """
    Extract crop(w:h:x:y) values from a named segment of the filtergraph.
    Returns (crop_w, crop_h, crop_x, crop_y).
    """
    import re
    # find the crop call in the segment that ends at the [label] output
    # e.g. "crop=1215:720:0:0,scale=..."
    pattern = rf"crop=(\d+):(\d+):(\d+):(\d+)"
    # Extract the portion up to [label]
    seg_match = re.search(rf"(.*?)\[{re.escape(label)}\]", filtergraph)
    if not seg_match:
        raise ValueError(f"Label [{label}] not found in filtergraph")
    seg = seg_match.group(1)
    # grab the last crop(...) in that segment
    all_crops = re.findall(pattern, seg)
    if not all_crops:
        raise ValueError(f"No crop() found before [{label}] in: {seg}")
    w, h, x, y = (int(v) for v in all_crops[-1])
    return w, h, x, y


class TestSplitScreenRealDims:
    """Verify build_split_screen_filter uses the real src_w/src_h."""

    # ---------------------------------------------------------------
    # 720p source  (1280 x 720)
    # ---------------------------------------------------------------
    def test_720p_crops_within_bounds(self):
        src_w, src_h = 1280, 720
        # Two people well separated in a 720p frame
        box_left  = (100,  80, 300, 500)  # (x, y, w, h)
        box_right = (880,  80, 300, 500)

        fg = build_split_screen_filter(box_left, box_right, src_w=src_w, src_h=src_h)

        crop_w1, crop_h1, crop_x1, _ = _parse_crop(fg, "top")
        crop_w2, crop_h2, crop_x2, _ = _parse_crop(fg, "bottom")

        # Crop widths must not exceed source width
        assert crop_w1 <= src_w, f"top crop_w {crop_w1} exceeds src_w {src_w}"
        assert crop_w2 <= src_w, f"bottom crop_w {crop_w2} exceeds src_w {src_w}"

        # Crop x offsets + widths must stay within source
        assert crop_x1 >= 0, f"top crop_x {crop_x1} is negative"
        assert crop_x2 >= 0, f"bottom crop_x {crop_x2} is negative"
        assert crop_x1 + crop_w1 <= src_w, (
            f"top crop out-of-bounds: x={crop_x1} w={crop_w1} src_w={src_w}"
        )
        assert crop_x2 + crop_w2 <= src_w, (
            f"bottom crop out-of-bounds: x={crop_x2} w={crop_w2} src_w={src_w}"
        )

        # Crop heights equal src_h (full vertical crop)
        assert crop_h1 == src_h, f"top crop_h {crop_h1} != src_h {src_h}"
        assert crop_h2 == src_h, f"bottom crop_h {crop_h2} != src_h {src_h}"

    # ---------------------------------------------------------------
    # 4K source  (3840 x 2160)
    # ---------------------------------------------------------------
    def test_4k_crops_within_bounds(self):
        src_w, src_h = 3840, 2160
        box_left  = (400,  200, 900, 1500)
        box_right = (2600, 200, 900, 1500)

        fg = build_split_screen_filter(box_left, box_right, src_w=src_w, src_h=src_h)

        for label in ("top", "bottom"):
            crop_w, crop_h, crop_x, _ = _parse_crop(fg, label)
            assert crop_w <= src_w, f"[{label}] crop_w {crop_w} > src_w {src_w}"
            assert crop_x >= 0,     f"[{label}] crop_x {crop_x} < 0"
            assert crop_x + crop_w <= src_w, (
                f"[{label}] crop out-of-bounds: x={crop_x} w={crop_w} src_w={src_w}"
            )
            assert crop_h == src_h, f"[{label}] crop_h {crop_h} != src_h {src_h}"

    # ---------------------------------------------------------------
    # Original 1080p — confirm unchanged behaviour
    # ---------------------------------------------------------------
    def test_1080p_crops_within_bounds(self):
        src_w, src_h = 1920, 1080
        box_left  = (100,  50, 400, 800)
        box_right = (1200, 50, 400, 800)

        fg = build_split_screen_filter(box_left, box_right, src_w=src_w, src_h=src_h)

        for label in ("top", "bottom"):
            crop_w, crop_h, crop_x, _ = _parse_crop(fg, label)
            assert crop_w <= src_w
            assert crop_x >= 0
            assert crop_x + crop_w <= src_w
            assert crop_h == src_h

    # ---------------------------------------------------------------
    # Edge case: person centred near left edge (x=0)
    # ---------------------------------------------------------------
    def test_person_at_left_edge_no_negative_x(self):
        src_w, src_h = 1280, 720
        box_left  = (0,   50, 200, 400)   # touching left edge
        box_right = (900, 50, 200, 400)

        fg = build_split_screen_filter(box_left, box_right, src_w=src_w, src_h=src_h)

        crop_w1, _, crop_x1, _ = _parse_crop(fg, "top")
        assert crop_x1 >= 0, f"crop_x1 went negative: {crop_x1}"
        assert crop_x1 + crop_w1 <= src_w

    # ---------------------------------------------------------------
    # Edge case: person centred near right edge
    # ---------------------------------------------------------------
    def test_person_at_right_edge_no_overflow(self):
        src_w, src_h = 1280, 720
        box_left  = (100,  50, 200, 400)
        box_right = (1100, 50, 200, 400)   # almost at right edge

        fg = build_split_screen_filter(box_left, box_right, src_w=src_w, src_h=src_h)

        crop_w2, _, crop_x2, _ = _parse_crop(fg, "bottom")
        assert crop_x2 >= 0
        assert crop_x2 + crop_w2 <= src_w, (
            f"bottom crop overflows: x={crop_x2} w={crop_w2} src_w={src_w}"
        )

    # ---------------------------------------------------------------
    # Verify vstack and scale appear in the filtergraph
    # ---------------------------------------------------------------
    def test_filtergraph_structure(self):
        fg = build_split_screen_filter(
            (100, 50, 400, 800), (1200, 50, 400, 800),
            src_w=1920, src_h=1080,
        )
        assert "vstack" in fg.lower(), "vstack missing from filtergraph"
        assert "scale=" in fg, "scale missing from filtergraph"
        assert "[outv]" in fg, "[outv] output label missing"
