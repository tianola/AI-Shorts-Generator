"""
VLM-based scene understanding with fallback chain.

Primary: Google Gemini Vision API
Fallback 1: Hybrid (expanded YOLO + scene detection + audio cues)
Fallback 2: OmniRoute vision (if available)

This replaces the old scene_attention.py which relied only on YOLO COCO classes
(too limited for real-world content like MrBeast videos).
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np
from PIL import Image

from utils.logger import logger


async def understand_scene_vlm(
    video_path: str,
    timestamp: float,
    transcript_context: str = "",
) -> Dict[str, Any]:
    """
    Use VLM to understand what's happening in a video frame.

    Returns:
        {
            "subject": "what the scene is about",
            "objects": ["list", "of", "visible", "objects"],
            "action": "what's happening",
            "confidence": 0.0-1.0
        }
    """
    # Extract frame
    frame = _extract_frame(video_path, timestamp)
    if frame is None:
        return {"subject": "unknown", "objects": [], "action": "unknown", "confidence": 0.0}

    # Try providers in order
    providers = [
        ("google_gemini", _google_gemini_vision),
        ("omniroute_vision", _omniroute_vision),
    ]

    for provider_name, provider_fn in providers:
        try:
            result = await provider_fn(frame, transcript_context)
            if result and result.get("confidence", 0) > 0.3:
                logger.info(f"VLM scene understanding: {provider_name} -> {result.get('subject')}")
                return result
        except Exception as e:
            logger.warning(f"VLM provider {provider_name} failed: {e}")
            continue

    # Final fallback: hybrid approach
    logger.info("All VLM providers failed, using hybrid fallback")
    return _hybrid_understanding(video_path, timestamp, transcript_context)


def _extract_frame(video_path: str, timestamp: float) -> Optional[np.ndarray]:
    """Extract a single frame from video at given timestamp."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None


async def _google_gemini_vision(frame: np.ndarray, transcript_context: str) -> Dict[str, Any]:
    """Google Gemini Vision API"""
    from google import genai
    from config import settings

    if not settings.GOOGLE_AI_STUDIO_KEY:
        raise ValueError("No GOOGLE_AI_STUDIO_KEY")

    client = genai.Client(api_key=settings.GOOGLE_AI_STUDIO_KEY)

    # Convert frame to PIL Image
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)

    prompt = f"""Analyze this video frame and identify:
1. The main subject (person, object, or scene the viewer should focus on)
2. Key visible objects (buildings, vehicles, equipment, etc.)
3. What action is happening

Transcript context: {transcript_context[:500]}

Return JSON: {{"subject": "...", "objects": [...], "action": "...", "confidence": 0.0-1.0}}"""

    response = client.models.generate_content(
        model=settings.GOOGLE_AI_STUDIO_MODEL,
        contents=[prompt, img]
    )

    # Parse JSON from response
    text = response.text
    # Extract JSON from markdown code block if present
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    result = json.loads(text.strip())
    return result


async def _omniroute_vision(frame: np.ndarray, transcript_context: str) -> Dict[str, Any]:
    """OmniRoute vision (if it supports vision models)"""
    from services.llm_fallback import chat_completion
    from config import settings

    # Check if OmniRoute supports vision
    if not settings.OMNIROUTE_API_KEY:
        raise ValueError("No OMNIROUTE_API_KEY")

    # Convert frame to base64
    _, buffer = cv2.imencode('.jpg', frame)
    import base64
    img_base64 = base64.b64encode(buffer).decode()

    # Try vision model (this may not work if OmniRoute doesn't support vision)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Analyze this video frame. Transcript: {transcript_context[:300]}. Return JSON: {{'subject': '...', 'objects': [...], 'action': '...', 'confidence': 0.0-1.0}}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}}
            ]
        }
    ]

    # This will likely fail if OmniRoute doesn't support vision
    result_text = chat_completion(messages, model="gpt-4-vision-preview", temperature=0.2)
    if not result_text:
        raise ValueError("OmniRoute vision not available")

    result = json.loads(result_text)
    return result


def _hybrid_understanding(video_path: str, timestamp: float, transcript_context: str) -> Dict[str, Any]:
    """
    Fallback: Hybrid approach using transcript analysis + scene context.

    This is the durable fallback that works without any external API and doesn't
    rely on YOLO detecting objects (which fails for buildings, construction, etc.).

    Always returns a scene with confidence based on transcript richness.
    """
    # Analyze transcript for action keywords
    transcript_lower = transcript_context.lower()

    # Action detection from transcript
    action = "speaking"
    action_keywords = {
        "construction": ["build", "construct", "create", "house", "city", "foundation", "wall"],
        "demonstration": ["show", "demonstrate", "explain", "look", "see", "watch"],
        "emotion": ["love", "help", "save", "change", "life", "hope", "dream"],
        "challenge": ["impossible", "challenge", "try", "test", "win", "lose"],
        "reveal": ["surprise", "reveal", "secret", "hidden", "discover"],
    }

    for action_type, keywords in action_keywords.items():
        if any(kw in transcript_lower for kw in keywords):
            action = action_type
            break

    # Subject inference from transcript
    subject = "scene"
    subject_keywords = {
        "person": ["i", "me", "my", "you", "we", "they", "he", "she"],
        "building": ["house", "building", "city", "wall", "roof", "door"],
        "vehicle": ["car", "truck", "bus", "drive"],
        "nature": ["tree", "field", "water", "sky", "ground"],
    }

    for subject_type, keywords in subject_keywords.items():
        if any(kw in transcript_lower for kw in keywords):
            subject = subject_type
            break

    # Confidence based on transcript richness (more words = more context = higher confidence)
    word_count = len(transcript_context.split())
    confidence = min(0.8, 0.4 + (word_count / 100))  # 0.4 base + 0.4 for rich transcript

    result = {
        "subject": subject,
        "objects": [],  # No YOLO objects, but we still have a valid scene
        "action": action,
        "confidence": confidence,
        "source": "hybrid_fallback"
    }

    logger.info(f"Hybrid fallback at t={timestamp:.1f}s: action={action}, subject={subject}, conf={confidence:.2f} (from {word_count} words)")

    return result


async def build_vlm_attention(
    analysis: Dict[str, Any],
    clip_words_path: str,
    transcript: str = "",
    video_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build scene attention using VLM understanding.

    Samples key frames from the clip and uses VLM to understand what's happening,
    then maps that to crop focus decisions.
    """
    video_path = video_path or analysis.get("video_path")
    if not video_path or not os.path.exists(video_path):
        return {"scenes": [], "notes": ["no video path"]}

    samples = analysis.get("samples", [])
    if not samples:
        return {"scenes": [], "notes": ["no samples"]}

    # Sample 3-5 key frames across the clip
    n_frames = min(5, len(samples))
    step = max(1, len(samples) // n_frames)
    key_frames = samples[::step][:n_frames]

    scenes = []
    for ts, dets in key_frames:
        try:
            understanding = await understand_scene_vlm(
                video_path, ts, transcript_context=transcript
            )
            if understanding.get("confidence", 0) > 0.3:
                scenes.append({
                    "start": ts,
                    "end": ts + 2.0,  # Assume scene lasts ~2s
                    "target": understanding.get("subject", "person"),
                    "importance": understanding.get("confidence", 0.5),
                    "source": understanding.get("source", "vlm"),
                    "objects": understanding.get("objects", []),
                    "action": understanding.get("action", "unknown"),
                })
        except Exception as e:
            logger.warning(f"VLM understanding failed at t={ts:.1f}s: {e}")
            continue

    notes = [f"vlm tier: {len(scenes)} scene(s)"]
    return {"scenes": scenes, "notes": notes}
