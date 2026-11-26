"""services/editing/ab.py: did each planned visual effect show up in the finished file?

The frame comparison itself (ffmpeg SSIM/PSNR) is tested in test_visual_regression.py. Here it is
replaced by a fake that returns a chosen SSIM/PSNR per moment, so the decision rules, the time
mapping through the anti-fingerprint speed change, the control moment and the report file are
checked without ffmpeg and without any network.
"""
import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import pytest

from services import visual_regression as vr
from services.editing import ab


def _track(start, end, kind="zoom_punch", event_id="e"):
    return {"kind": kind, "start": start, "end": end, "intensity": 0.8, "event_id": event_id}


class FakeCompare:
    """Stands in for visual_regression.compare_video_frames.

    `metrics` maps a time in the plain clip to (ssim, psnr). Any time not listed gets the
    default (a control-like frame: very close to plain). It records the pairs it was asked for.
    """

    def __init__(self, metrics=None, default=(0.999, 50.0)):
        self.metrics = metrics or {}
        self.default = default
        self.pairs = None

    def __call__(self, a_video, b_video, pairs, work_dir):
        self.pairs = list(pairs)
        rows = []
        for ta, tb in pairs:
            ssim, psnr = self.metrics.get(round(ta, 2), self.default)
            rows.append({"time_a": round(ta, 2), "time_b": round(tb, 2), "ssim": ssim, "psnr": psnr})
        return rows


@pytest.fixture()
def fake(monkeypatch):
    holder = {}

    def install(**kwargs):
        compare = FakeCompare(**kwargs)
        monkeypatch.setattr(vr, "compare_video_frames", compare)
        holder["compare"] = compare
        return compare

    install.holder = holder
    return install


# ---------------------------------------------------------------- control moment

def test_control_time_is_none_for_a_clip_under_two_seconds():
    assert ab._control_time(1.9, []) is None


def test_control_time_stays_clear_of_every_effect():
    tracks = [_track(0.5, 3.0), _track(6.0, 7.0)]
    t = ab._control_time(10.0, tracks)
    assert t is not None
    assert 0.0 < t < 10.0
    for tr in tracks:
        assert t < tr["start"] - 0.4 or t > tr["end"] + 0.4


def test_control_time_is_none_when_effects_cover_the_clip():
    # Every candidate moment sits inside the effect, so no clear control exists.
    assert ab._control_time(10.0, [_track(0.0, 10.0)]) is None


# ---------------------------------------------------------------- visible decision

def test_effect_is_visible_when_structure_drops_below_control(fake):
    # control ~ (0.999, 50 dB); the effect frame drops SSIM well below it.
    compare = fake(metrics={5.0: (0.90, 48.0)})
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(4.0, 6.0)], 10.0, 1.0, "work")
    assert report["items"][0]["visible"] is True
    assert compare.pairs[0] == (5.0, 5.0)


def test_colour_only_effect_is_caught_by_psnr_not_ssim(fake):
    # A tint barely moves SSIM but drops PSNR well past the margin: still visible.
    fake(metrics={5.0: (0.9995, 33.0)})
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(4.0, 6.0)], 10.0, 1.0, "work")
    item = report["items"][0]
    assert item["ssim"] > 0.99
    assert item["visible"] is True


def test_effect_within_both_margins_is_not_visible(fake):
    # Control is (0.999, 50 dB). This frame is 0.0035 below it in SSIM (margin 0.005)
    # and 1.0 dB below it in PSNR (margin 1.5): inside both margins, so not shown.
    fake(metrics={5.0: (0.9955, 49.0)})
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(4.0, 6.0)], 10.0, 1.0, "work")
    assert report["items"][0]["visible"] is False


def test_failed_frame_gives_visible_none(fake):
    fake(metrics={5.0: (None, None)})
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(4.0, 6.0)], 10.0, 1.0, "work")
    assert report["items"][0]["visible"] is None


def test_no_control_gives_visible_none_for_every_effect(fake):
    # A clip too short for a control moment: nothing can be judged.
    fake(metrics={0.5: (0.5, 10.0)})
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(0.0, 1.0)], 1.0, 1.0, "work")
    assert report["control"] is None
    assert report["items"][0]["visible"] is None


# ---------------------------------------------------------------- time mapping and limits

def test_final_time_is_clip_time_divided_by_speed(fake):
    # The anti-fingerprint step speeds the shipped file up by 2%: clip 5.0 s is 5.0/1.02 s there.
    compare = fake()
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(4.0, 6.0)], 10.0, 1.02, "work")
    assert compare.pairs[0][0] == pytest.approx(5.0)
    assert compare.pairs[0][1] == pytest.approx(5.0 / 1.02, abs=0.01)
    assert report["items"][0]["time_final"] == pytest.approx(5.0 / 1.02, abs=0.01)
    assert report["speed"] == 1.02


@pytest.mark.parametrize("speed", [0, -1.0, None])
def test_bad_speed_is_treated_as_one(fake, speed):
    compare = fake()
    report = ab.ab_report("plain.mp4", "final.mp4", [_track(4.0, 6.0)], 10.0, speed, "work")
    assert report["speed"] == 1.0
    assert compare.pairs[0][1] == pytest.approx(compare.pairs[0][0])


def test_at_most_twelve_effects_are_checked_in_time_order(fake):
    compare = fake()
    tracks = [_track(float(i), float(i) + 0.5, event_id=f"e{i}") for i in range(20)]
    tracks.reverse()  # out of order on purpose
    report = ab.ab_report("plain.mp4", "final.mp4", tracks, 30.0, 1.0, "work")
    assert len(report["items"]) == ab.MAX_ITEMS
    starts = [item["time_clip"] for item in report["items"]]
    assert starts == sorted(starts)
    assert starts[0] == pytest.approx(0.25)  # midpoint of the earliest effect


def test_items_carry_their_event_and_kind(fake):
    fake()
    report = ab.ab_report("plain.mp4", "final.mp4",
                          [_track(1.0, 2.0, kind="shake", event_id="ev-7")], 10.0, 1.0, "work")
    assert report["items"][0]["event_id"] == "ev-7"
    assert report["items"][0]["kind"] == "shake"


def test_no_effects_gives_no_items(fake):
    fake()
    report = ab.ab_report("plain.mp4", "final.mp4", [], 10.0, 1.0, "work")
    assert report["items"] == []


# ---------------------------------------------------------------- report file

def test_write_report_writes_a_readable_json(tmp_path):
    report = {"control": {"time_clip": 8.0, "ssim": 0.999, "psnr": 50.0}, "speed": 1.0,
              "items": [{"kind": "flash", "visible": True}]}
    ab.write_report(str(tmp_path), report)
    path = tmp_path / ab.AB_FILE
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == report
