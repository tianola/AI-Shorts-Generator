"""Frame comparison with tolerances, for visual regression and for A/B checks.

Two frames are compared with ffmpeg's own `ssim` and `psnr` filters (no extra dependency), after
both are scaled to the same small size so a rendering difference in one pixel row does not decide
the result. A baseline is a folder of approved frames plus baseline.json; a render passes when
every frame is at or above the tolerances. Tolerances are deliberately not 1.0: x264 and font
hinting differ slightly between machines, and a regression that matters (a title in the wrong
place, a missing caption, a black edge) moves SSIM far more than that.
"""

import json
import os
import re
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

COMPARE_SIZE = (270, 480)
SSIM_MIN = 0.97
PSNR_MIN = 28.0
FFMPEG_TIMEOUT_S = 60
BASELINE_FILE = "baseline.json"

_SSIM = re.compile(r"All:([\d.]+)")
_PSNR = re.compile(r"average:([\d.]+|inf)")


def _run(cmd: List[str]) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=FFMPEG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"Visual comparison command failed: {exc}")
        return None


def extract_frame(video: str, at: float, out_path: str, size: Optional[Tuple[int, int]] = None) -> bool:
    """One frame of `video` at `at` seconds, written as an image (scaled to `size` when given).
    False when it could not be cut."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, at):.3f}", "-i", video,
           "-frames:v", "1"]
    if size:
        cmd += ["-vf", f"scale={size[0]}:{size[1]}:flags=bicubic"]
    result = _run(cmd + [out_path])
    return bool(result and result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0)


def compare_images(a: str, b: str, size: Tuple[int, int] = COMPARE_SIZE) -> Optional[Dict[str, float]]:
    """{ssim, psnr} between two images (1.0 and 100 dB for identical ones), or None on failure."""
    scale = f"scale={size[0]}:{size[1]}:flags=bicubic,format=yuv420p"
    out: Dict[str, float] = {}
    for name, flt, pattern in (("ssim", "ssim", _SSIM), ("psnr", "psnr", _PSNR)):
        result = _run(["ffmpeg", "-hide_banner", "-i", a, "-i", b, "-lavfi",
                       f"[0:v]{scale}[x];[1:v]{scale}[y];[x][y]{flt}", "-f", "null", "-"])
        if result is None or result.returncode != 0:
            return None
        found = pattern.findall(result.stderr or "")
        if not found:
            return None
        out[name] = 100.0 if found[-1] == "inf" else float(found[-1])
    return {"ssim": round(out["ssim"], 4), "psnr": round(min(out["psnr"], 100.0), 2)}


def compare_video_frames(a_video: str, b_video: str, pairs: Sequence[Tuple[float, float]],
                         work_dir: str) -> List[Dict]:
    """For each (time in a, time in b): the SSIM and PSNR of the two frames. Failed frames are None."""
    os.makedirs(work_dir, exist_ok=True)
    rows = []
    for n, (ta, tb) in enumerate(pairs):
        fa, fb = os.path.join(work_dir, f"a_{n}.png"), os.path.join(work_dir, f"b_{n}.png")
        metrics = None
        if extract_frame(a_video, ta, fa) and extract_frame(b_video, tb, fb):
            metrics = compare_images(fa, fb)
        rows.append({"time_a": round(ta, 2), "time_b": round(tb, 2),
                     "ssim": metrics["ssim"] if metrics else None,
                     "psnr": metrics["psnr"] if metrics else None})
    return rows


def capture_baseline(video: str, times: Sequence[float], out_dir: str, label: str = "",
                     size: Optional[Tuple[int, int]] = None) -> Dict:
    """Approve the current render: write its frames and baseline.json into `out_dir`."""
    os.makedirs(out_dir, exist_ok=True)
    frames = []
    for n, at in enumerate(times):
        name = f"frame_{n}.png"
        if not extract_frame(video, at, os.path.join(out_dir, name), size):
            raise RuntimeError(f"could not cut a frame at {at}s")
        frames.append({"time": round(float(at), 3), "file": name})
    record = {"label": label, "frames": frames}
    with open(os.path.join(out_dir, BASELINE_FILE), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return record


def check_baseline(video: str, baseline_dir: str, work_dir: str,
                   ssim_min: float = SSIM_MIN, psnr_min: float = PSNR_MIN) -> Dict:
    """Compare `video` with an approved baseline: {passed, results: [{time, ssim, psnr, passed}]}."""
    with open(os.path.join(baseline_dir, BASELINE_FILE), "r", encoding="utf-8") as handle:
        record = json.load(handle)
    os.makedirs(work_dir, exist_ok=True)
    results = []
    for n, frame in enumerate(record["frames"]):
        current = os.path.join(work_dir, f"current_{n}.png")
        metrics = None
        if extract_frame(video, frame["time"], current):
            metrics = compare_images(os.path.join(baseline_dir, frame["file"]), current)
        ok = bool(metrics and metrics["ssim"] >= ssim_min and metrics["psnr"] >= psnr_min)
        results.append({"time": frame["time"], "ssim": metrics["ssim"] if metrics else None,
                        "psnr": metrics["psnr"] if metrics else None, "passed": ok})
    return {"passed": bool(results) and all(r["passed"] for r in results),
            "ssim_min": ssim_min, "psnr_min": psnr_min, "results": results}
