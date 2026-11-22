"""One call for the worker: check the shipped clip and record how it was rendered.

assess_clip() runs the technical evaluator on the final file, builds the render manifest, writes
quality.json and render_manifest.json next to the clip, and returns the values the worker stores on
the clip row. It never raises: a failure here must not lose a rendered clip.
"""

import json
import os
from typing import Dict, List, Optional, Sequence

from loguru import logger

from services import render_manifest
from services.quality.models import QualityReport
from services.quality.technical import evaluate_clip

QUALITY_FILE = "quality.json"
MAX_CAPTION_EVENTS = 400


def caption_events_from_words(words_file: str) -> List[Dict[str, float]]:
    """[{start, end}] in clip time for every caption word, from the clip's words.json."""
    try:
        with open(words_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("words", [])
    events = []
    for word in data if isinstance(data, list) else []:
        try:
            start = float(word.get("start", word.get("start_time")))
            end = float(word.get("end", word.get("end_time")))
        except (AttributeError, TypeError, ValueError):
            continue
        if end > start:
            events.append({"start": start, "end": end})
    return events[:MAX_CAPTION_EVENTS]


def assess_clip(
    final_path: str,
    clip_dir: str,
    *,
    job_id: int,
    clip_index: int,
    clip_start: float,
    clip_end: float,
    words_file: str,
    source_path: Optional[str] = None,
    layout: Optional[str] = None,
    caption_style: Optional[str] = None,
    caption_motion: Optional[str] = None,
    edit_profile: Optional[str] = None,
    tone: Optional[str] = None,
    sticker_paths: Sequence[str] = (),
    sfx_paths: Sequence[str] = (),
    font_paths: Sequence[str] = (),
    filtergraph: Optional[str] = None,
    expect_audio: bool = True,
    speed: float = 1.0,
    anti_fingerprint: Optional[Dict] = None,
    caption_events: Optional[List[Dict[str, float]]] = None,
) -> Optional[Dict]:
    """{'report': dict, 'manifest': dict, 'fingerprint': str}, or None when it could not be done."""
    try:
        events = caption_events if caption_events is not None else caption_events_from_words(words_file)
        report: QualityReport = evaluate_clip(final_path, {
            "expect_audio": expect_audio,
            "expected_duration": max(0.0, clip_end - clip_start),
            "speed": speed,
            "caption_events": events,
        })
        with open(os.path.join(clip_dir, QUALITY_FILE), "w", encoding="utf-8") as handle:
            handle.write(report.model_dump_json(indent=2))

        manifest = render_manifest.build_manifest(
            job_id=job_id, clip_index=clip_index, clip_start=clip_start, clip_end=clip_end,
            source_path=source_path, layout=layout, caption_style=caption_style,
            caption_motion=caption_motion, edit_profile=edit_profile, tone=tone,
            timeline_path=os.path.join(clip_dir, "edit_timeline.json"),
            sticker_paths=sticker_paths, sfx_paths=sfx_paths, font_paths=font_paths,
            filtergraph=filtergraph, anti_fingerprint=anti_fingerprint,
        )
        render_manifest.write_manifest(clip_dir, manifest)
        return {
            "report": report.model_dump(mode="json"),
            "manifest": manifest,
            "fingerprint": render_manifest.manifest_fingerprint(manifest),
        }
    except Exception as exc:
        logger.warning(f"Quality assessment skipped: {exc}")
        return None
