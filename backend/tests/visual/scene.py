"""A small deterministic scene for visual regression: synthetic source, title, caption, two effects.

Everything in it is generated (no real footage), so the same code gives the same frames on any
machine. `render_scene` is used both by scripts/visual_baseline.py (to approve a baseline) and by the
regression test (to render again and compare).
"""
import os
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "scripts"))

DURATION = 4.0
# (name, time in seconds): the frames that are compared
FRAME_TIMES = [0.5, 1.6, 2.7, 3.5]
EFFECTS = [
    {"kind": "zoom_punch", "start": 1.4, "end": 2.0, "intensity": 0.8, "event_id": "e1"},
    {"kind": "tint_red", "start": 2.5, "end": 3.0, "intensity": 0.9, "event_id": "e2"},
]
BASELINE_SIZE = (270, 480)


def render_scene(work_dir: str, title: str = "Visual regression scene", effects=EFFECTS) -> str:
    from benchmark_render import write_captions
    from services.render import render_single_pass

    os.makedirs(work_dir, exist_ok=True)
    source = os.path.join(work_dir, "source.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc2=size=1920x1080:rate=30:duration={DURATION}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source],
        check=True, timeout=120)
    captions = os.path.join(work_dir, "captions.ass")
    write_captions(captions, DURATION)
    out = os.path.join(work_dir, "scene.mp4")
    render_single_pass(source, 0.0, DURATION, out, captions_path=captions, title_text=title or None,
                       mode="static", effects=effects, emoji_stickers=[])
    return out
