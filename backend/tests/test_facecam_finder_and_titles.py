"""Webcam finder: a person who stays in a corner is the webcam; titles keep the topic."""

import numpy as np

from services import facecam_finder as ff
from services import live_studio as ls
from services.gaming_layout import panel_for_face


def _frames(n: int, w: int = 1280, h: int = 720):
    return [np.zeros((h, w, 3), dtype=np.uint8) for _ in range(n)]


def _patch(monkeypatch, frames, people_for_frame):
    monkeypatch.setattr(ff, "_sample_frames", lambda path, n: (frames, 1280, 720))
    monkeypatch.setattr(ff, "_people", lambda fs: [people_for_frame(i) for i in range(len(fs))])


def test_person_in_a_corner_on_most_frames_is_the_webcam(monkeypatch):
    frames = _frames(24)
    # the streamer in the top-left webcam on every frame; a passing NPC in the middle on a few
    _patch(monkeypatch, frames, lambda i: [(123, 80, 108, 134)] + ([(500, 300, 90, 200)] if i < 5 else []))
    cams = ff.find_webcams("unused.mp4")
    assert len(cams) == 1
    assert cams[0].region == "top-left" and cams[0].hit_rate == 1.0
    x, y, w, h = cams[0].box
    assert 120 <= x <= 180 and 70 <= y <= 110          # head and shoulders, not the whole body


def test_person_seen_rarely_is_not_a_webcam(monkeypatch):
    frames = _frames(24)
    _patch(monkeypatch, frames, lambda i: [(1100, 300, 60, 120)] if i < 4 else [])
    assert ff.find_webcams("unused.mp4") == []
    assert ff.find_webcam("unused.mp4") is None


def test_huge_person_in_a_corner_is_not_a_webcam(monkeypatch):
    frames = _frames(24)
    # a player's own body close to the camera fills a corner: too big to be a webcam
    _patch(monkeypatch, frames, lambda i: [(781, 236, 500, 300)])
    assert ff.find_webcams("unused.mp4") == []


def test_panel_is_centred_on_the_face_and_inside_the_frame():
    left, top, width, height = panel_for_face((145, 81, 55, 46), 1280, 720)
    assert left >= 0 and top >= 0
    assert abs(width / height - 1080 / 672) < 0.02
    assert left + width <= 1280 and top + height <= 720


def test_title_keeps_the_topic():
    assert ls._keep_topic("GTA 6 bank heist fail", "GTA 6") == "GTA 6 bank heist fail"
    assert ls._keep_topic("Hotel robbery chaos", "GTA 6") == "GTA 6: Hotel robbery chaos"
    assert ls._keep_topic("Hotel robbery chaos", "") == "Hotel robbery chaos"


def test_heuristic_title_uses_the_topic():
    hit = ls.heuristic_highlight("this is insane, no way! wow!! what?!", 0, 20, context="Some stream",
                                 topic="GTA 6")
    assert hit is not None and hit.title.startswith("GTA 6:")


def test_live_capture_prefers_h264_video():
    """AV1 video cannot go into the MPEG-TS buffer, so the capture asks for H.264 first."""
    assert "avc1" in ls.LIVE_FORMAT
    assert ls.LIVE_FORMAT.startswith("bv*[height<=1080][vcodec^=avc1]")
