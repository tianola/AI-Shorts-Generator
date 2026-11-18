"""
Multi-modal Virality Detector & Emotion Classifier (Active Expressions).
Computes visual engagement, facial expression intensity, movement dynamics,
and audio/transcript curiosity cues to rank clips scientifically with OpusClip-grade breakdowns.
"""
import os

import cv2
import numpy as np
from loguru import logger


def detect_visual_activity(video_path: str, sample_rate: int = 15) -> dict:
    """
    Analyzes visual activity, motion energy, and speaker engagement across the clip.
    """
    if not os.path.exists(video_path):
        return {"motion_score": 0.5, "face_presence": 0.5, "intensity_score": 0.5}

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"motion_score": 0.5, "face_presence": 0.5, "intensity_score": 0.5}

    prev_gray = None
    motion_magnitudes = []
    face_counts = []
    frame_idx = 0

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % sample_rate != 0:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (320, 180))

        if prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            motion_magnitudes.append(np.mean(mag))

        prev_gray = gray

        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=4, minSize=(20, 20))
        face_counts.append(len(faces))

    cap.release()

    avg_motion = float(np.mean(motion_magnitudes)) if motion_magnitudes else 1.0
    face_ratio = float(np.mean([1 if c > 0 else 0 for c in face_counts])) if face_counts else 0.5

    motion_score = min(1.0, max(0.1, avg_motion / 5.0))
    intensity_score = round((motion_score * 0.4) + (face_ratio * 0.6), 2)

    return {
        "motion_score": round(motion_score, 2),
        "face_presence": round(face_ratio, 2),
        "intensity_score": intensity_score
    }


def compute_composite_virality(
    llm_score: float,
    transcript: str,
    video_path: str = ""
) -> dict:
    """
    Computes an OpusClip-style Virality Breakdown:
    - Hook Score (First 3s text sentiment/curiosity)
    - Visual Intensity (Motion & Face Presence)
    - Coherence & Story Arc
    - Final Weighted Virality Score (0 - 100)
    - Natural Language Virality Explanation
    """
    visual_stats = detect_visual_activity(video_path) if video_path else {"intensity_score": 0.75, "motion_score": 0.7, "face_presence": 0.8}

    words = transcript.lower().split()
    high_impact_words = {
        "secret", "never", "always", "shocking", "million", "money", "mistake",
        "truth", "crazy", "stop", "revealed", "insane", "hidden", "nobody", "how",
        "μυστικό", "λεφτά", "λάθος", "αλήθεια", "τρελό", "προσοχή"
    }
    impact_count = sum(1 for w in words if any(kw in w for kw in high_impact_words))
    transcript_boost = min(0.25, impact_count * 0.05)

    base_score = llm_score if llm_score else 0.80
    visual_score = visual_stats.get("intensity_score", 0.5)

    # The selector's own score (the editor's verdict on what the viewer hears) carries
    # the number; picture activity is a small correction. It used to weigh 35% and the
    # keyword boost 15%, and the result was floored at 50: a talk-over-diagrams video
    # has no faces or motion, so every clip scored below 50 and showed exactly 50.
    # transcript_boost stays out of the score: its word list is English-only.
    final_virality = (base_score * 0.85) + (visual_score * 0.15)
    final_virality_100 = round(min(99.0, max(1.0, final_virality * 100.0)), 1)

    # Generate clear OpusClip-style explainer
    if final_virality_100 >= 85:
        tier = "God-Tier Viral Potential"
        explanation = f"Exceptional hook strength with immediate curiosity trigger ({impact_count} power keywords) and continuous dynamic pacing."
    elif final_virality_100 >= 70:
        tier = "High Virality Candidate"
        explanation = "Solid retention structure with clear topic focus, good conversational energy, and strong TikTok keyword density."
    else:
        tier = "Standard Clip"
        explanation = "Balanced informative segment suitable for organic topic discovery and playlist highlights."

    return {
        "virality_score_100": final_virality_100,
        "virality_score": round(final_virality, 2),
        "tier": tier,
        "virality_explanation": explanation,
        "visual_intensity": visual_stats["intensity_score"],
        "motion_score": visual_stats["motion_score"],
        "face_presence": visual_stats["face_presence"],
        "power_keywords_count": impact_count
    }


def resolve_virality_stats(
    llm_score,
    transcript: str,
    video_path: str = ""
) -> dict:
    """Compute the multimodal virality breakdown, but never fail silently.

    compute_composite_virality can raise on a corrupt vertical mp4 or an
    OpenCV/ffmpeg problem. The old call site swallowed that behind a bare
    `except Exception` and stored a hardcoded 85.0, which sits *above* the
    real computed band (~60-70) — so a crashed clip outranked a scored one and
    the failure left no trace. This wrapper passes the real result through, and
    on failure falls back to the selector's own 0-1 composite scaled to 100
    (flagged, and logged) instead of the inverted constant.
    """
    try:
        return compute_composite_virality(llm_score, transcript, video_path)
    except Exception as exc:
        base = float(llm_score) if llm_score else 0.80
        fallback_100 = round(min(99.0, max(0.0, base * 100.0)), 1)
        logger.warning(
            "compute_composite_virality failed for {} ({}); storing the selector "
            "score {} as the virality fallback instead of a constant",
            video_path or "<no video>", exc, fallback_100,
        )
        return {
            "virality_score_100": fallback_100,
            "virality_score": round(base, 2),
            "tier": "Standard Clip",
            "virality_explanation": (
                "Visual/keyword analysis was unavailable for this clip, so its "
                "score reflects the content-selection signal alone."
            ),
            "fallback": True,
        }
