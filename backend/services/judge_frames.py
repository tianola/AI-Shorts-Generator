"""Keyframes for the judge pass, so it scores what a viewer sees and not only hears.

The judge reads the words inside each candidate window. A clip whose first
second is a black frame, a slide or an empty chair reads as well as one that
opens on a face mid-sentence, and only the frames tell them apart. This module
grabs a few small JPEGs per candidate and turns them into Claude image blocks.

Claude only: the OmniRoute chain gets the text prompt unchanged.
"""
import asyncio
import base64
import os
import subprocess
from typing import List, Optional

from loguru import logger

# 384 px wide is enough to see faces, text on screen and motion blur, and keeps
# one image at a few hundred input tokens.
FRAME_WIDTH = 384
FRAME_TIMEOUT_SECONDS = 20


def sample_times(start: float, end: float, count: int) -> List[float]:
    """`count` times inside [start, end]: the opening second first, the rest spread out.

    The opening frame is what decides the swipe, so it is always the first one.
    """
    if count <= 0 or end <= start:
        return []
    first = min(start + 0.5, end)
    if count == 1:
        return [round(first, 2)]
    step = (end - first) / count
    return [round(first + i * step, 2) for i in range(count)]


def _grab_jpeg(video_path: str, at: float) -> Optional[bytes]:
    cmd = [
        "ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", video_path,
        "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"Judge frame at {at:.1f}s failed: {exc}")
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def _frames_blocks_sync(video_path: str, clips: list, per_clip: int) -> list:
    blocks = []
    for index, clip in enumerate(clips):
        start, end = float(clip["start"]), float(clip["end"])
        images = []
        times = sample_times(start, end, per_clip)
        for at in times:
            jpeg = _grab_jpeg(video_path, at)
            if jpeg:
                images.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": base64.standard_b64encode(jpeg).decode("ascii")},
                })
        if not images:
            continue
        offsets = ", ".join(f"+{at - start:.0f}s" for at in times[:len(images)])
        blocks.append({"type": "text", "text": f"Frames from candidate c{index} at {offsets}:"})
        blocks.extend(images)
    if not blocks:
        return []
    return [{"type": "text", "text": (
        "Below are frames from each candidate, the first one at its opening second. "
        "Use them for what the viewer sees: a face in frame, something happening, or a "
        "static, blank or unreadable screen. An opening frame with nothing worth stopping "
        "for lowers hook and pacing."
    )}] + blocks


async def judge_frame_blocks(video_path: Optional[str], clips: list, per_clip: int) -> list:
    """Claude content blocks with `per_clip` frames per candidate, or [] when unavailable."""
    if not video_path or per_clip <= 0 or not clips or not os.path.exists(video_path):
        return []
    try:
        return await asyncio.to_thread(_frames_blocks_sync, video_path, clips, per_clip)
    except Exception as exc:
        logger.warning(f"Could not sample judge frames ({type(exc).__name__}: {exc}); judging on words only")
        return []
