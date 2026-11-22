"""Colour emoji stickers: PNG rendering, the sidecar list, the overlay graph, and the burn-in.

Tests that need a colour emoji font skip when none is installed, and tests that run ffmpeg
skip when it is not on PATH. Sticker PNGs are cached under STORAGE_ROOT, so every test that
renders one points STORAGE_ROOT at its own temp directory.
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image, ImageChops, ImageStat

from config import settings
from services import emoji_stickers
from services.emoji_stickers import MAX_STICKERS_PER_CLIP

MONEY = "\U0001F4B0"
FIRE = "\U0001F525"
TEST_TUBE = "\U0001F9EA"

STATIC_VERTICAL = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"

needs_font = pytest.mark.skipif(
    not emoji_stickers.available(), reason="no colour emoji font installed on this host"
)
needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="requires ffmpeg on PATH"
)
FFMPEG_TIMEOUT_S = 120


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    return tmp_path


def _sticker(emoji=MONEY, start=0.5, end=1.5, x=540.0, y=1300.0, size=120):
    return {"emoji": emoji, "x": x, "y": y, "size": size, "start": start, "end": end}


def _make_png(path, size=(128, 128)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", size, (255, 0, 0, 255)).save(path)
    return path


# ---------------------------------------------------------------------------
# sticker_png
# ---------------------------------------------------------------------------
@needs_font
def test_sticker_png_renders_colour_128px_rgba(storage):
    path = emoji_stickers.sticker_png(MONEY)
    assert path is not None
    assert os.path.dirname(path) == os.path.join(str(storage), "assets", "emoji")
    with Image.open(path) as img:
        assert img.mode == "RGBA"
        assert img.size == (128, 128)
        colours = img.getcolors(maxcolors=128 * 128)
    assert colours is not None
    opaque = {colour for _count, colour in colours if colour[3] == 255}
    assert len(opaque) > 50, f"only {len(opaque)} distinct opaque colours; not a colour emoji"


@needs_font
def test_sticker_png_second_call_reuses_cache(storage):
    first = emoji_stickers.sticker_png(MONEY)
    os.utime(first, (1_000_000, 1_000_000))
    second = emoji_stickers.sticker_png(MONEY)
    assert second == first
    assert os.path.getmtime(second) == 1_000_000  # not re-rendered


def test_sticker_png_returns_none_without_font(storage, monkeypatch):
    monkeypatch.setattr(emoji_stickers, "emoji_font_path", lambda: None)
    assert emoji_stickers.sticker_png(TEST_TUBE) is None
    assert not os.path.exists(os.path.join(str(storage), "assets", "emoji", "1f9ea.png"))


# ---------------------------------------------------------------------------
# sidecar: write_sidecar / load_stickers
# ---------------------------------------------------------------------------
@needs_font
def test_sidecar_round_trip_returns_existing_pngs(storage, tmp_path):
    captions = str(tmp_path / "captions.ass")
    emoji_stickers.write_sidecar(captions, [_sticker(MONEY), _sticker(FIRE, 2.0, 3.0)])
    loaded = emoji_stickers.load_stickers(captions)
    assert [item["emoji"] for item in loaded] == [MONEY, FIRE]
    for item in loaded:
        assert os.path.isfile(item["path"])
        assert item["size"] == 120


@needs_font
def test_sidecar_load_caps_at_max_stickers(storage, tmp_path):
    captions = str(tmp_path / "captions.ass")
    many = [_sticker(MONEY, float(i), float(i) + 0.5) for i in range(MAX_STICKERS_PER_CLIP + 2)]
    emoji_stickers.write_sidecar(captions, many)
    with open(emoji_stickers.sidecar_path(captions), encoding="utf-8") as handle:
        assert len(json.load(handle)["stickers"]) == MAX_STICKERS_PER_CLIP
    assert len(emoji_stickers.load_stickers(captions)) <= MAX_STICKERS_PER_CLIP


def test_write_sidecar_empty_list_removes_file(tmp_path):
    captions = str(tmp_path / "captions.ass")
    emoji_stickers.write_sidecar(captions, [_sticker()])
    assert os.path.isfile(emoji_stickers.sidecar_path(captions))
    emoji_stickers.write_sidecar(captions, [])
    assert not os.path.exists(emoji_stickers.sidecar_path(captions))


def test_load_stickers_missing_or_corrupt_file_returns_empty(tmp_path):
    captions = str(tmp_path / "captions.ass")
    assert emoji_stickers.load_stickers(None) == []
    assert emoji_stickers.load_stickers(captions) == []  # no sidecar at all
    with open(emoji_stickers.sidecar_path(captions), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert emoji_stickers.load_stickers(captions) == []


# ---------------------------------------------------------------------------
# build_overlay_graph
# ---------------------------------------------------------------------------
def test_overlay_graph_two_stickers_chain_labels_and_timing():
    stickers = [
        {**_sticker(MONEY, 0.5, 2.0, 200.0, 300.0, 96), "path": "a.png"},
        {**_sticker(FIRE, 1.0, 3.0, 800.0, 900.0, 96), "path": "b.png"},
    ]
    extra, parts, last, next_idx = emoji_stickers.build_overlay_graph(stickers, 3, "[vsub]")

    assert [item["kind"] for item in extra] == ["emoji", "emoji"]
    assert [item["path"] for item in extra] == ["a.png", "b.png"]
    assert next_idx == 3 + 2

    overlays = [part for part in parts if "overlay=" in part]
    assert len(overlays) == 2
    assert "enable='between(t,0.500,2.000)'" in overlays[0]
    assert "enable='between(t,1.000,3.000)'" in overlays[1]
    # Inputs are read by index starting at first_input_idx.
    assert "[3:v]" in parts[0]
    assert "[4:v]" in parts[2]
    # Each overlay reads the previous label and the chain ends on the final label.
    assert overlays[0].startswith("[vsub][em0]overlay=")
    assert overlays[1].startswith("[vem0][em1]overlay=")
    assert last == "[vem1]"
    assert overlays[1].endswith("[vem1]")


def test_overlay_graph_empty_passes_label_through():
    assert emoji_stickers.build_overlay_graph([], 4, "[vsub]") == ([], [], "[vsub]", 4)


# ---------------------------------------------------------------------------
# build_single_pass_filter with stickers
# ---------------------------------------------------------------------------
def test_single_pass_filter_includes_sticker_overlay(tmp_path):
    from services.render import build_single_pass_filter
    from test_filtergraphs import _assert_valid

    png = _make_png(str(tmp_path / "sticker.png"))
    stickers = [{**_sticker(MONEY, 0.5, 2.0, 540.0, 1300.0, 120), "path": png}]
    extra, fc, vmap, _amap = build_single_pass_filter(
        STATIC_VERTICAL, captions_path=None, emoji_stickers=stickers,
    )

    emoji_inputs = [item for item in extra if item["kind"] == "emoji"]
    assert len(emoji_inputs) == 1
    assert emoji_inputs[0]["path"] == png
    assert "overlay=" in fc
    assert fc.endswith("format=yuv420p[vout]")
    assert vmap == "[vout]"
    _assert_valid(fc, "single pass with emoji sticker")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg on PATH")
def test_single_pass_sticker_graph_runs_in_ffmpeg(tmp_path):
    from services.render import build_single_pass_filter

    png = _make_png(str(tmp_path / "sticker.png"))
    stickers = [{**_sticker(MONEY, 0.5, 1.5, 540.0, 1300.0, 120), "path": png}]
    extra, fc, vmap, _amap = build_single_pass_filter(
        STATIC_VERTICAL, emoji_stickers=stickers, has_audio=False,
    )
    out = str(tmp_path / "graph_check.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2"]
    for item in extra:
        cmd += ["-loop", "1", "-i", item["path"]]
    cmd += ["-filter_complex", fc, "-map", vmap, "-t", "2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", out]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert os.path.getsize(out) > 0


# ---------------------------------------------------------------------------
# Title position
# ---------------------------------------------------------------------------
def test_title_center_frac_per_mode():
    from services import gaming_layout
    from services.render import _title_center_frac

    assert _title_center_frac("gaming") == pytest.approx(1.0 - gaming_layout.PANEL_SPLIT)
    assert _title_center_frac("gaming") == pytest.approx(0.35)
    assert _title_center_frac("single_speaker") == 0.3333


def test_title_overlay_y_follows_center_frac():
    from services.render import build_single_pass_filter
    from test_filtergraphs import _assert_valid

    _, fc, _, _ = build_single_pass_filter(
        STATIC_VERTICAL, title_png="x.png", title_center_frac=0.35,
    )
    assert "H*0.3500-h/2" in fc
    assert r"clip(t/0.45\,0\,1)" in fc  # commas escaped in the unquoted overlay y=
    _assert_valid(fc, "title overlay at gaming centre")

    _, fc_default, _, _ = build_single_pass_filter(STATIC_VERTICAL, title_png="x.png")
    assert "H*0.3333-h/2" in fc_default


# ---------------------------------------------------------------------------
# End to end: captions with a sticker burned by render.burn_captions
# ---------------------------------------------------------------------------
def _run_ffmpeg(args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                   check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_S)


def _frame_rgb(video, t, png_path):
    _run_ffmpeg(["-i", video, "-ss", f"{t:.3f}", "-frames:v", "1", png_path])
    with Image.open(png_path) as img:
        return img.convert("RGB")


@needs_font
@needs_ffmpeg
def test_burned_in_sticker_appears_at_its_time(storage, tmp_path, monkeypatch):
    from services import render
    from services.captions import create_captions

    monkeypatch.setattr(settings, "CAPTION_EMOJI_MODE", "sticker")

    video = str(tmp_path / "source.mp4")
    _run_ffmpeg(["-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=3",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", video])

    words = [
        {"word": "Make", "start": 0.0, "end": 0.6},
        {"word": "money", "start": 0.6, "end": 1.5},
        {"word": "today", "start": 1.5, "end": 2.5},
    ]
    words_path = str(tmp_path / "words.json")
    with open(words_path, "w", encoding="utf-8") as handle:
        json.dump(words, handle)
    ass = str(tmp_path / "captions.ass")
    create_captions(words_path, ass, style_name="mrbeast")

    with open(emoji_stickers.sidecar_path(ass), encoding="utf-8") as handle:
        stickers = json.load(handle)["stickers"]
    assert len(stickers) == 1 and stickers[0]["emoji"] == MONEY
    st = stickers[0]

    out = str(tmp_path / "burned.mp4")
    render.burn_captions(video, ass, out)

    sample_t = st["start"] + 0.3
    during = _frame_rgb(out, sample_t, str(tmp_path / "during.png"))
    before = _frame_rgb(out, 0.0, str(tmp_path / "before.png"))
    assert during.size == (1080, 1920)

    half = st["size"] / 2.0
    box = (
        max(0, int(st["x"] - half)), max(0, int(st["y"] - half)),
        min(1080, int(st["x"] + half)), min(1920, int(st["y"] + half)),
    )
    region_during = during.crop(box)
    region_before = before.crop(box)

    # Saturated (non-grey) pixels are present in the sticker area.
    r, g, b = region_during.split()
    chroma = ImageChops.subtract(
        ImageChops.lighter(ImageChops.lighter(r, g), b),
        ImageChops.darker(ImageChops.darker(r, g), b),
    )
    saturated = sum(chroma.histogram()[41:])
    assert saturated > 0

    # The sticker changes the area compared with the frame before it appears.
    diff = ImageStat.Stat(ImageChops.difference(region_during, region_before)).mean
    mean_abs_diff = sum(diff) / len(diff)
    assert mean_abs_diff > 5, f"sticker area barely changed (mean abs diff {mean_abs_diff:.2f})"


def test_a_sidecar_with_the_wrong_json_shape_is_ignored(tmp_path):
    from services import emoji_stickers

    captions = str(tmp_path / "c.ass")
    for payload in ("[]", '"text"', '{"stickers": 5}', '{"stickers": [1, "x", {"no": "emoji"}]}'):
        with open(emoji_stickers.sidecar_path(captions), "w", encoding="utf-8") as handle:
            handle.write(payload)
        assert emoji_stickers.load_stickers(captions) == []
