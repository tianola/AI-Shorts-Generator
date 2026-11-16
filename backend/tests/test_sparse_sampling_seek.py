"""Sparse whole-source sampling seeks instead of decoding every frame.

Job 112's temporal sweep took one sample per 11.4 s of a 46 min source but
decoded all ~82k frames to get there (about 11 of the stage's 16 minutes).
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import FFMPEG_REQUIRED
from services import focus_tracking


@FFMPEG_REQUIRED
def test_seek_sampling_lands_on_the_same_timestamps_as_the_sweep(tmp_path, monkeypatch):
    video = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=12",
                    "-c:v", "libx264", "-g", "50", str(video)], check=True)
    frames_seen = []

    def fake_detect(frame, conf=0.25):
        frames_seen.append(frame.shape)
        return []

    monkeypatch.setattr(focus_tracking, "detect_objects_in_frame", fake_detect)

    sparse = focus_tracking.sample_frames(str(video), sample_interval=focus_tracking.SEEK_SAMPLE_INTERVAL)
    monkeypatch.setattr(focus_tracking, "SEEK_SAMPLE_INTERVAL", 1e9)
    swept = focus_tracking.sample_frames(str(video), sample_interval=2.0)

    assert [round(ts, 3) for ts, _ in sparse] == [round(ts, 3) for ts, _ in swept]
    assert [round(ts, 1) for ts, _ in sparse] == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    assert all(shape == (180, 320, 3) for shape in frames_seen)
