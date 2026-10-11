"""
Smart Visuals Service — High-level facade uniting Tone Gating, Contextual Image Retrieval,
and Animated Pop-up Card Generation for Short-form Videos.
"""
import os
from typing import Optional, Dict, Any, List
from loguru import logger
from services.tone_detector import is_transcript_somber, classify_content_tone, TONE_TAXONOMY
from services.image_fetcher import get_contextual_image, get_contextual_image_sync
from services.visual_popup import create_styled_popup_card, build_popup_filtergraph_fragment


async def prepare_clip_popup(
    transcript: str,
    clip_cues: List[Dict[str, Any]],
    output_dir: str,
    clip_duration: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """
    Evaluates tone and processes the best visual popup for a clip.

    Returns:
        Dict with keys:
            - 'card_path': str (path to styled PNG card)
            - 'start': float
            - 'duration': float
            - 'position': str
            - 'query': str
        or None if not eligible or fetch fails.
    """
    # 1. Deterministic tone safety gating
    if is_transcript_somber(transcript):
        logger.info("Smart Visuals: Popup skipped due to somber/sensitive content.")
        return None

    tone_info = classify_content_tone(transcript)
    if not tone_info.get("popup_eligible", True):
        logger.info(f"Smart Visuals: Popup skipped for tone '{tone_info.get('tone_category')}'.")
        return None

    if not clip_cues:
        logger.debug("Smart Visuals: No visual cues provided for clip.")
        return None

    # Take primary cue
    cue = clip_cues[0]
    query = cue.get("query", "").strip()
    if not query:
        return None

    # Timing safety constraints: avoid first 1.8s and last 2.0s
    start_offset = float(cue.get("start_offset", 3.0))
    start_offset = max(1.8, min(start_offset, max(1.8, clip_duration - 3.5)))
    duration = float(cue.get("duration", 2.0))
    duration = max(1.5, min(duration, 2.5))
    position = cue.get("position", "upper_third")

    # 2. Fetch image via waterfall
    raw_img = await get_contextual_image(query)
    if not raw_img:
        logger.info(f"Smart Visuals: No contextual image found for query '{query}'.")
        return None

    # 3. Render styled popup card PNG
    os.makedirs(output_dir, exist_ok=True)
    card_path = os.path.join(output_dir, "popup_card.png")
    styled_card = create_styled_popup_card(raw_img, card_path)

    return {
        "card_path": styled_card,
        "start": start_offset,
        "duration": duration,
        "position": position,
        "query": query,
        "tone": tone_info.get("tone_category", "casual_podcast"),
    }
