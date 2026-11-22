"""Approve (capture) or check the visual-regression baseline for the synthetic scene.

    python scripts/visual_baseline.py capture   # renders the scene and writes tests/visual/baselines/scene
    python scripts/visual_baseline.py check     # renders again and compares with the approved frames

Capture only after looking at the frames: they become the reference every later render is held to.
"""
import json
import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests", "visual"))

from scene import BASELINE_SIZE, FRAME_TIMES, render_scene  # noqa: E402
from services import visual_regression as vr  # noqa: E402

BASELINE_DIR = os.path.join(BACKEND, "tests", "visual", "baselines", "scene")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    with tempfile.TemporaryDirectory() as work:
        video = render_scene(work)
        if mode == "capture":
            record = vr.capture_baseline(video, FRAME_TIMES, BASELINE_DIR, label="synthetic scene", size=BASELINE_SIZE)
            print(json.dumps(record, indent=2))
            return 0
        report = vr.check_baseline(video, BASELINE_DIR, os.path.join(work, "cmp"))
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
