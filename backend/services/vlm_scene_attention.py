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
from typing import Any, Dict, List, Optional, Tuple
import base64
import cv2
import httpx
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

    from config import settings

    # Providers in preference order. When the local Ollama vision tier is enabled
    # it runs FIRST: it answers on this machine with no account and none of the
    # remote timeouts, so a live clip does not burn seconds per frame on Gemini
    # (credits) and OmniRoute (model not served) that are currently dead.
    providers = []
    if getattr(settings, "LOCAL_VLM_ENABLED", False):
        providers.append(("local_ollama", _local_vlm))
    providers += [
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


async def _local_vlm(frame: np.ndarray, transcript_context: str) -> Dict[str, Any]:
    """Local Ollama vision tier — the no-account fallback that names what COCO misses.

    Posts the sampled frame to the running Ollama server's /api/generate (the same
    shape the text router uses) with the image base64-encoded in ``images``. It
    needs a vision-capable model pulled (LOCAL_VLM_MODEL); a missing server or
    model raises and the caller moves to the next tier, so enabling it can never
    hard-fail a clip.
    """
    from config import settings

    # JPEG → base64 (Ollama takes raw base64, no data: prefix).
    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        raise ValueError("could not encode frame for Ollama")
    img_b64 = base64.b64encode(buffer).decode()

    # A plain description prompt: the CPU-friendly tier here is moondream, which
    # returns prose and is finicky — the phrasing "What is shown in the image?"
    # is the one verified to give full captions on real frames; longer asks make
    # it truncate or answer empty. format=json must NOT be set (it collapses a
    # caption model to a couple of tokens). An instruction-following model that
    # answers in JSON is still handled (see the try below).
    prompt = "What is shown in the image?"
    if transcript_context:
        prompt = f"Spoken context: {transcript_context[:150]}\n\n{prompt}"

    payload = {
        "model": settings.LOCAL_VLM_MODEL,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {"temperature": 0.2},
    }

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(f"{settings.LOCAL_VLM_URL.rstrip('/')}/api/generate", json=payload)
        response.raise_for_status()
        body = response.json()

    answer = (body.get("response") or "").strip()
    if not answer:
        raise ValueError("Ollama returned an empty answer")
    try:
        scene = _extract_json_obj(answer)
    except (ValueError, json.JSONDecodeError):
        scene = _caption_to_scene(answer)
    scene.setdefault("subject", "person")
    scene.setdefault("objects", [])
    scene.setdefault("action", "unknown")
    scene.setdefault("confidence", 0.5)
    scene["caption"] = answer
    scene["source"] = "local_ollama"
    return scene


# Scene types a caption can name that the COCO detector cannot — mapped to a
# short label the reframe/hook layers can act on, checked most-specific first.
_SCENE_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("construction", ("construction", "crane", "scaffold", "excavat", "demolition", "renovat", "hard hat")),
    ("building", ("building", "house", "skyscraper", "tower", "warehouse", "roof", "facade", "storefront", "room", "interior", "kitchen")),
    ("nature", ("field", "forest", "tree", "mountain", "lake", "river", "beach", "ocean", "sky", "garden", "grass")),
    ("vehicle", ("car", "truck", "bus", "van", "motorcycle", "bike", "boat", "train")),
    ("crowd", ("crowd", "people gathered", "group of people", "audience")),
    ("food", ("food", "cook", "meal", "plate", "ingredient", "carrot", "grater", "prepares")),
    ("animal", ("dog", "cat", "horses", "cow", "bird")),
    ("person", ("person", "people", "man", "woman", "child", "boy", "girl")),
]


def _caption_to_scene(caption: str) -> Dict[str, Any]:
    """Turn a free-text frame caption into a scene dict.

    Confidence rises with how much the model actually described (a real caption is
    far more trustworthy than the transcript heuristics); a near-empty reply is
    dropped below the 0.3 gate so the caller falls through to the hybrid tier.
    """
    lower = caption.lower()
    subject = next((name for name, keys in _SCENE_KEYWORDS if any(k in lower for k in keys)), "scene")
    objects = [name for name, keys in _SCENE_KEYWORDS if any(k in lower for k in keys) and name != subject]
    words = len(caption.split())
    confidence = 0.0 if words < 3 else min(0.85, 0.5 + words / 40)
    return {"subject": subject, "objects": objects, "action": "shown", "confidence": round(confidence, 3)}


def _extract_json_obj(text: str) -> Dict[str, Any]:
    """Parse a JSON object out of a model reply that may wrap it in prose/fences."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Ollama returned an empty answer")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError(f"Ollama answer was not JSON: {text[:120]}")


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
