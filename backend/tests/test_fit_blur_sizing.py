"""Defect 3 — is the fit_blur backdrop sized to the 9:16 output canvas?

The report claimed the blurred layer was being built at 1920x1080. It is not:
``build_blurred_fit_filter`` derives the backdrop from ``target_w``/``target_h``
(half canvas, blurred, upscaled) and never reads ``src_w``/``src_h`` at all.
These tests pin the real contract so the claim can be checked instead of
re-litigated: every ``scale``/``crop`` in the graph is portrait and even, and a
render through the path lands on exactly 1080x1920 whatever the source shape.
"""
import re
import shutil
import subprocess

import pytest

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

CANVAS = (1080, 1920)
TIMEOUT = 180


def _size_pairs(frag):
    """Every (w, h) pair handed to scale=/crop= in the fragment.

    A ``-2`` height is ffmpeg's "auto, but even" — it is even by construction,
    so it belongs in the same check as the literal sizes.
    """
    return [(int(m.group(1)), int(m.group(2)))
            for m in re.finditer(r"(?:scale|crop)=(\d+):(-?\d+)", frag)]


def test_blurred_backdrop_is_sized_to_the_output_canvas():
    from services.focus_tracking import build_blurred_fit_filter

    frag = build_blurred_fit_filter(src_w=1920, src_h=1080, target_w=1080, target_h=1920)

    # the backdrop: half canvas to blur, then up to the full canvas
    assert "scale=540:960" in frag, frag
    assert "crop=540:960" in frag, frag
    assert "scale=1080:1920" in frag, frag
    # nothing in the graph is a landscape canvas
    assert "1920:1080" not in frag, frag


def test_blurred_fit_dimensions_are_even_everywhere():
    """libx264 + yuv420p rejects an odd width/height on the final stream, and a
    -2 scale factor is what keeps the foreground even."""
    from services.focus_tracking import build_blurred_fit_filter

    for target in ((1080, 1920), (720, 1280), (1440, 2560)):
        frag = build_blurred_fit_filter(src_w=1920, src_h=1080,
                                        target_w=target[0], target_h=target[1])
        for w, h in _size_pairs(frag):
            assert w % 2 == 0 and h % 2 == 0, f"odd dimension in {target}: {frag}"
        assert f"scale={target[0]}:-2" in frag, frag


def test_vertical_fit_blur_fragment_carries_the_canvas_size():
    """build_vertical_filter's fit_blur branch must emit the same 9:16 canvas it
    is asked for, whatever the probe of the source says."""
    from services import vertical
    from services.focus_tracking import build_blurred_fit_filter

    expected = build_blurred_fit_filter(src_w=640, src_h=480,
                                        target_w=1080, target_h=1920).replace("[outv]", "[vsingle]")
    got = vertical.build_vertical_filter("dummy.mp4", mode="fit_blur",
                                         target_width=1080, target_height=1920)
    assert got == expected, f"\nexpected {expected}\n got     {got}"


def _probe_dims(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True, timeout=60).stdout
    w, h = [int(x) for x in out.strip().split(",")[:2]]
    return w, h


def _make_source(path, size):
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={size[0]}x{size[1]}:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
        capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr[-500:]
    return path


@needs_ffmpeg
@pytest.mark.parametrize("src_size", [(1920, 1080), (1280, 720), (882, 1176), (1080, 1920)])
def test_fit_blur_render_lands_on_the_canvas(tmp_path, src_size):
    """The end-to-end contract, measured on a real encode.

    882x1176 is the awkward one: it is not 16:9, so a backdrop built from the
    source shape would come out at a different size — and an odd source edge is
    exactly what used to reach the encoder.
    """
    from services.render import render_single_pass
    from services.focus_tracking import build_blurred_fit_filter

    src = _make_source(str(tmp_path / f"src_{src_size[0]}x{src_size[1]}.mp4"), src_size)
    frag = build_blurred_fit_filter(src_w=src_size[0], src_h=src_size[1],
                                    target_w=CANVAS[0], target_h=CANVAS[1])
    out = str(tmp_path / "final.mp4")
    render_single_pass(src, 0.0, 2.0, out,
                       vertical_frag=frag.replace("[outv]", "[vsingle]"))

    w, h = _probe_dims(out)
    assert (w, h) == CANVAS, f"{src_size} source rendered to {w}x{h}"
    assert w % 2 == 0 and h % 2 == 0


@needs_ffmpeg
def test_fit_blur_wired_into_the_single_pass_graph_still_emits_one_canvas_stream(tmp_path):
    """fit_blur consumes [0:v] twice, so wiring it behind another stage needs
    the split the fragment already carries — a duplicate pad kills the graph."""
    from services.render import build_single_pass_filter
    from services.focus_tracking import build_blurred_fit_filter

    frag = build_blurred_fit_filter(src_w=1920, src_h=1080,
                                    target_w=1080, target_h=1920).replace("[outv]", "[vsingle]")
    _, fc, vmap, _ = build_single_pass_filter(frag, has_audio=True)

    src = _make_source(str(tmp_path / "src.mp4"), (1920, 1080))
    out = str(tmp_path / "out.mp4")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-filter_complex", fc, "-map", vmap, "-an",
         "-frames:v", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
        capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0, r.stderr[-800:]
    assert _probe_dims(out) == CANVAS
