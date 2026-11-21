"""Branding config/command and the gaming facecam layout."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import branding, gaming_layout as gl  # noqa: E402


# ------------------------------------------------------------------ branding

def test_clean_fills_defaults_and_clamps_ranges():
    cfg = branding.clean({"logo_size": 99, "logo_opacity": -4, "logo_position": "nowhere",
                          "text": "x" * 500, "bogus": 1})
    assert cfg["logo_size"] == 20 and cfg["logo_opacity"] == 10
    assert cfg["logo_position"] == "top-right" and len(cfg["text"]) == 120
    assert "bogus" not in cfg and cfg["enabled"] is False


def test_nothing_configured_means_no_command():
    assert branding.build_command("in.mp4", "out.mp4", {"enabled": True}) is None


def test_a_logo_is_overlaid_with_opacity_and_size(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"x")
    cmd = branding.build_command("in.mp4", "out.mp4", {"logo_enabled": True, "logo_path": str(logo),
                                                       "logo_size": 10, "logo_opacity": 50,
                                                       "logo_position": "bottom-left"})
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "colorchannelmixer=aa=0.500" in graph and "scale=108:-1" in graph
    assert "overlay=x=40:y=(H-h-40)" in graph


def test_background_key_out_and_text_box(tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"x")
    cmd = branding.build_command(
        "in.mp4", "out.mp4",
        {"logo_enabled": True, "logo_path": str(logo), "logo_bg_remove": True, "logo_bg_color": "#00ff00",
         "text_enabled": True, "text": "@channel", "text_bg_enabled": True, "text_color": "#ff0000"},
        text_file=str(tmp_path / "t.txt"))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "colorkey=0x00FF00" in graph
    assert "drawtext=textfile=" in graph and "fontcolor=0xFF0000" in graph and "box=1" in graph


def test_save_and_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(branding.settings, "STORAGE_ROOT", str(tmp_path))
    branding.save({"enabled": True, "text_enabled": True, "text": "hello"})
    assert branding.load()["text"] == "hello" and branding.load()["enabled"] is True


# -------------------------------------------------------------------- gaming

def test_a_corner_face_is_a_facecam_and_a_centre_face_is_not():
    assert gl.corner_of((1500, 780, 300, 260), 1920, 1080) == "bottom-right"
    assert gl.corner_of((100, 40, 250, 250), 1920, 1080) == "top-left"
    assert gl.corner_of((800, 400, 300, 300), 1920, 1080) is None       # centre
    assert gl.corner_of((0, 0, 1200, 700), 1920, 1080) is None           # too wide
    # IShowSpeed's overlay: a 56 px face on the left edge, half-way down a 1280x720 stream
    assert gl.corner_of((85, 355, 56, 56), 1280, 720) == "left-edge"
    assert gl.corner_of((300, 300, 280, 280), 1920, 1080) is None       # a big face on the side is a speaker


def test_the_facecam_must_hold_its_corner_on_most_frames():
    cam = (1500, 780, 300, 260)
    steady = [[cam]] * 8 + [[]] * 2
    rect = gl.pick_facecam(steady, 1920, 1080)
    assert rect is not None and rect[0] < 1500 and rect[0] + rect[2] <= 1920
    assert gl.pick_facecam([[cam]] * 3 + [[]] * 7, 1920, 1080) is None
    assert gl.pick_facecam([], 1920, 1080) is None


def test_the_gaming_filter_stacks_gameplay_over_the_facecam():
    graph = gl.build_gaming_filter(1920, 1080, (1400, 700, 400, 300))
    assert graph.endswith("[game][camp]vstack=inputs=2[outv]")
    assert "crop=400:300:1400:700" in graph and "scale=1080:-2[gf]" in graph


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_gaming_filter_renders_a_1080x1920_frame(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=10:duration=1",
                    str(src)], check=True, capture_output=True)
    out = tmp_path / "out.mp4"
    graph = gl.build_gaming_filter(1920, 1080, (1400, 700, 400, 300))
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", graph, "-map", "[outv]", str(out)],
                   check=True, capture_output=True)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1920"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_branding_burns_a_text_watermark_into_a_real_video(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=10:duration=1",
                    "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:a", "aac", str(src)],
                   check=True, capture_output=True)
    dst = tmp_path / "branded.mp4"
    assert branding.apply_branding(str(src), str(dst), {"text_enabled": True, "text": "@mychannel"})
    assert dst.stat().st_size > 1024


def test_a_small_overlay_facecam_gets_a_panel_shaped_window_inside_the_frame():
    rect = gl.pick_facecam([[(85, 355, 56, 56)]] * 8, 1280, 720)
    x, y, w, h = rect
    assert x >= 0 and y >= 0 and x + w <= 1280 and y + h <= 720
    assert abs(w / h - gl.PANEL_ASPECT) < 0.05
    assert x <= 85 and x + w >= 85 + 56 and y <= 355 and y + h >= 355 + 56      # the face is inside it
    assert w >= 56 * 3                                                             # and room around it


def test_the_cover_layout_puts_the_webcam_on_top_and_fills_the_gameplay_below():
    graph = gl.build_gaming_filter(1280, 720, (0, 306, 258, 162), cover=True)
    assert graph.endswith("[camp][game]vstack=inputs=2[outv]")
    assert "crop=258:162:0:306" in graph and "boxblur" not in graph
