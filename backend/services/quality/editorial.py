"""Editorial review of a finished clip: what a viewer would see in fixed frames.

The technical evaluator measures the file. This one looks at it. It cuts a fixed set of frames,
sends them with the title and a short description of the planned edits to an image-capable model,
and records what the model observes. Rules that keep it honest:

- Every finding cites a frame by number and quotes what is visible in it. A finding whose frame
  does not exist, or that quotes nothing, is dropped.
- The same frames go to the model several times. A finding counts as `stable` only when at least
  two runs report it for the same frame and category, and the share of findings that repeat is
  stored as the report's `agreement_rate`.
- Findings are advisory. They never change the clip's status or hold it back, until their
  precision has been checked against clips a person has reviewed.
- There is no score. The model is asked what looks wrong, not how good the clip is.
"""

import json
import os
import re
import subprocess
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from services.quality.models import CATEGORIES, Evidence, Finding, has_evidence

EVALUATOR_VERSION = "editorial-1"
FRAME_FRACTIONS = {3: (0.04, 0.5, 0.96), 4: (0.04, 0.35, 0.65, 0.96), 5: (0.04, 0.25, 0.5, 0.75, 0.96),
                   6: (0.04, 0.2, 0.4, 0.6, 0.8, 0.96), 7: (0.04, 0.18, 0.33, 0.5, 0.67, 0.82, 0.96),
                   8: (0.04, 0.16, 0.29, 0.43, 0.57, 0.71, 0.84, 0.96)}
FRAME_WIDTH = 540
FRAME_TIMEOUT_S = 60
FRAMES_DIR = "editorial_frames"
EDITORIAL_FILE = "editorial.json"
EDITORIAL_CATEGORIES = tuple(c for c in CATEGORIES if c in ("captions", "visual", "editing", "hook", "title"))
MAX_FINDINGS_PER_RUN = 8

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "frame", "issue", "explanation", "observed", "confidence", "recommendation"],
        "properties": {
            "category": {"type": "string", "enum": list(EDITORIAL_CATEGORIES)},
            "frame": {"type": "integer"},
            "issue": {"type": "string"},
            "explanation": {"type": "string"},
            "observed": {"type": "string"},
            "confidence": {"type": "number"},
            "recommendation": {"type": "string"},
        },
    }}},
}

SYSTEM = (
    "You review frames from a finished vertical short video before it is published. "
    "Report only problems you can point at in a specific frame: text overlapping other text or the "
    "speaker's face, a title or caption cut off or unreadable, an emoji or effect covering something "
    "important, a frame that is empty or confusing, a title that does not match what the frames show. "
    "Do not rate the clip, do not praise it, do not guess about things the frames do not show "
    "(audio, pacing, retention). If you see no problem, return an empty list. "
    "Every finding must name its frame number and say in \"observed\" what is visible there."
)


def extract_frames(path: str, duration: float, out_dir: str, count: int = 5) -> List[Dict]:
    """Fixed-position JPEG frames: [{index, time, path}]. Frames that fail to cut are left out."""
    fractions = FRAME_FRACTIONS.get(count, FRAME_FRACTIONS[5])
    os.makedirs(out_dir, exist_ok=True)
    frames = []
    for index, fraction in enumerate(fractions, start=1):
        at = round(max(0.0, duration * fraction), 2)
        target = os.path.join(out_dir, f"frame_{index}.jpg")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", path,
               "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", target]
        try:
            subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"Editorial frame {index} not cut: {exc}")
            continue
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            frames.append({"index": index, "time": at, "path": target})
    return frames


def build_prompt(frames: List[Dict], title: str, tone: str, edits: str, transcript: str) -> str:
    times = ", ".join(f"frame {f['index']} at {f['time']}s" for f in frames)
    return (
        f"The frames are labelled: {times}.\n"
        f"Burned-in title the clip should show: \"{title}\"\n"
        f"Content tone: {tone or 'unknown'}\n"
        f"Edits planned for this clip: {edits or 'none recorded'}\n"
        f"What is said in the clip: \"{(transcript or '')[:800]}\"\n\n"
        "Return the problems you can see, each tied to one frame."
    )


def parse_findings(raw: str, frames: List[Dict]) -> List[Finding]:
    """Validated findings from one run. Anything without a real frame and a quoted observation is dropped."""
    by_index = {f["index"]: f for f in frames}
    data = json.loads(raw)
    findings: List[Finding] = []
    for n, item in enumerate((data.get("findings") if isinstance(data, dict) else None) or [], start=1):
        if not isinstance(item, dict) or len(findings) >= MAX_FINDINGS_PER_RUN:
            continue
        frame = by_index.get(item.get("frame"))
        category = item.get("category")
        observed = str(item.get("observed") or "").strip()
        issue = str(item.get("issue") or "").strip()
        if frame is None or category not in EDITORIAL_CATEGORIES or not observed or not issue:
            continue
        if not os.path.isfile(frame["path"]):
            continue
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            continue
        evidence = Evidence(kind="frame", ref=frame["path"], value=observed[:300])
        if not has_evidence(evidence):
            continue
        findings.append(Finding(
            id=f"editorial-{category}-{frame['index']}-{n}",
            category=category, severity="info", source="editorial",
            issue=issue[:200], explanation=str(item.get("explanation") or "")[:400],
            confidence=confidence, evidence=evidence,
            recommendation=str(item.get("recommendation") or "")[:300],
            timestamp_clip=None, timestamp_final=frame["time"],
            evaluator_version=EVALUATOR_VERSION,
        ))
    return findings


def _key(finding: Finding) -> Tuple[str, str]:
    return finding.category, os.path.basename(finding.evidence.ref)


def aggregate_runs(runs: List[List[Finding]]) -> Tuple[List[Finding], Optional[float]]:
    """One list of findings from repeat runs, each marked stable or not, and the agreement rate.

    The same problem is recognised by its category and frame. `stable` needs at least two runs.
    The agreement rate is the share of distinct problems that two or more runs reported; it is None
    when fewer than two runs succeeded (no repeat to compare), and 1.0 when several runs all found nothing.
    """
    if not runs:
        return [], None
    seen: Dict[Tuple[str, str], List[Finding]] = {}
    for run in runs:
        keys_in_run = set()
        for finding in run:
            key = _key(finding)
            if key in keys_in_run:
                continue                      # a run repeating itself is not agreement
            keys_in_run.add(key)
            seen.setdefault(key, []).append(finding)
    merged = []
    for group in seen.values():
        best = max(group, key=lambda f: f.confidence)
        merged.append(best.model_copy(update={
            "runs_seen": len(group),
            "stable": len(group) >= 2,
            "confidence": round(sum(f.confidence for f in group) / len(group), 2),
        }))
    merged.sort(key=lambda f: (-f.runs_seen, -f.confidence, f.id))
    if len(runs) < 2:
        return merged, None
    if not merged:
        return [], 1.0
    return merged, round(sum(1 for f in merged if f.stable) / len(merged), 2)


async def _default_caller(prompt: str, frames: List[Dict]) -> str:
    from services.claude_client import call_claude_vision_json
    return await call_claude_vision_json(
        prompt=prompt, system=SYSTEM, schema=SCHEMA,
        image_paths=[(f"Frame {f['index']} ({f['time']}s):", f["path"]) for f in frames])


async def evaluate_editorial(
    final_path: str,
    clip_dir: str,
    *,
    duration: float,
    title: str = "",
    tone: str = "",
    edits: str = "",
    transcript: str = "",
    runs: int = 3,
    frame_count: int = 5,
    caller: Optional[Callable[[str, List[Dict]], Awaitable[str]]] = None,
) -> Optional[Dict]:
    """Run the editorial review and write editorial.json. None when it could not run; never raises.

    `caller(prompt, frames)` replaces the model call in tests.
    """
    try:
        frames = extract_frames(final_path, duration, os.path.join(clip_dir, FRAMES_DIR), frame_count)
        if len(frames) < 3:
            logger.warning("Editorial review skipped: fewer than three frames could be cut")
            return None
        prompt = build_prompt(frames, title, tone, edits, transcript)
        call = caller or _default_caller
        results: List[List[Finding]] = []
        errors: List[str] = []
        for _ in range(max(1, runs)):
            try:
                results.append(parse_findings(await call(prompt, frames), frames))
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}"[:200])
                logger.warning(f"Editorial run failed: {errors[-1]}")
        if not results:
            return None
        findings, agreement = aggregate_runs(results)
        report = {
            "version": EVALUATOR_VERSION,
            "advisory": True,
            "runs_requested": max(1, runs),
            "runs_completed": len(results),
            "run_errors": errors,
            "agreement_rate": agreement,
            "frames": [{"index": f["index"], "time": f["time"], "path": f["path"]} for f in frames],
            "findings": [f.model_dump(mode="json") for f in findings],
        }
        with open(os.path.join(clip_dir, EDITORIAL_FILE), "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        return report
    except Exception as exc:
        logger.warning(f"Editorial review skipped: {type(exc).__name__}: {exc}")
        return None


def describe_edits(timeline_path: str) -> str:
    """A one-line description of the planned edits, from edit_timeline.json."""
    try:
        with open(timeline_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    kinds: Dict[str, int] = {}
    for event in data.get("events", []) if isinstance(data, dict) else []:
        for action in (event.get("visual_actions") or []) + (event.get("audio_actions") or []):
            name = action.get("kind") if isinstance(action, dict) else str(action)
            if name:
                kinds[name] = kinds.get(name, 0) + 1
    profile = data.get("profile") if isinstance(data, dict) else ""
    parts = ", ".join(f"{n}x {k}" for k, n in sorted(kinds.items()))
    return f"profile {profile}; {parts}" if parts else f"profile {profile}" if profile else ""
