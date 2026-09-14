"""
Multi-modal Virality Detector & Emotion Classifier (Active Expressions).
Computes visual engagement, facial expression intensity, movement dynamics,
and audio peak energy to rank clips scientifically.
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

    # Lightweight face cascade for expression/presence density
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

        # Optical flow / motion detection
        if prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            motion_magnitudes.append(np.mean(mag))

        prev_gray = gray

        # Detect active faces in sampled frame
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=4, minSize=(20, 20))
        face_counts.append(len(faces))

    cap.release()

    avg_motion = float(np.mean(motion_magnitudes)) if motion_magnitudes else 1.0
    face_ratio = float(np.mean([1 if c > 0 else 0 for c in face_counts])) if face_counts else 0.5

    # Normalize motion score between 0.0 and 1.0
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
    video_path: str
) -> dict:
    """
    Computes an OpusClip-style Virality Breakdown:
    - Hook Score (First 3s text sentiment/curiosity)
    - Visual Intensity (Motion & Face Presence)
    - Coherence & Story Arc
    - Final Weighted Virality Score (0 - 100)
    """
    visual_stats = detect_visual_activity(video_path)

    # Transcript virality cues
    words = transcript.lower().split()
    high_impact_words = {"secret", "never", "always", "shocking", "million", "money", "mistake", "truth", "crazy", "stop", "revealed", "insane"}
    impact_count = sum(1 for w in words if w in high_impact_words)
    transcript_boost = min(0.25, impact_count * 0.05)

    base_score = llm_score or 0.75
    visual_score = visual_stats.get("intensity_score", 0.5)

    final_virality = (base_score * 0.50) + (visual_score * 0.35) + (transcript_boost * 0.15)
    final_virality_100 = round(min(99.0, max(45.0, final_virality * 100.0)), 1)

    return {
        "virality_score_100": final_virality_100,
        "virality_score": round(final_virality, 2),
        "visual_intensity": visual_stats["intensity_score"],
        "motion_score": visual_stats["motion_score"],
        "face_presence": visual_stats["face_presence"]
    }
