"""Visual regression: a generated scene (title, caption, zoom punch, red tint) must keep looking the
way it was approved. Frames are compared with SSIM and PSNR at tolerances (services/visual_regression).

To approve a deliberate change: look at the new frames, then run `python scripts/visual_baseline.py capture`.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scene import render_scene  # noqa: E402
from services import visual_regression as vr  # noqa: E402

BASELINE = os.path.join(HERE, "baselines", "scene")


@pytest.fixture(scope="module")
def scene_video(tmp_path_factory):
    return render_scene(str(tmp_path_factory.mktemp("scene")))


def test_the_scene_matches_its_approved_baseline(scene_video, tmp_path):
    report = vr.check_baseline(scene_video, BASELINE, str(tmp_path))
    assert report["passed"], report


def test_a_missing_title_is_caught(tmp_path):
    video = render_scene(str(tmp_path / "no_title"), title="")
    report = vr.check_baseline(video, BASELINE, str(tmp_path / "cmp"))
    assert not report["passed"], report


def test_a_missing_effect_is_caught(tmp_path):
    video = render_scene(str(tmp_path / "no_fx"), effects=None)
    report = vr.check_baseline(video, BASELINE, str(tmp_path / "cmp"))
    failed_times = [r["time"] for r in report["results"] if not r["passed"]]
    assert failed_times, report          # at least one effect frame no longer matches
