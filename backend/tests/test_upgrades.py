"""
Test suite for all upgraded services:
- MediaPipe Face Tracking
- Instructor Structured LLM Outputs
- Arq Worker Configuration
- Speaker Diarization Service
- AI B-Roll Service
"""
import numpy as np
from services.vertical import detect_faces_in_frame
from services.diarization import diarize_audio
from services.broll_ai import generate_ai_broll
from worker import WorkerSettings

def test_mediapipe_face_detection():
	"""Test MediaPipe face detection on a dummy frame (should execute cleanly)."""
	# Create a 640x480 dummy image
	dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
	boxes = detect_faces_in_frame(dummy_frame)
	assert isinstance(boxes, list)

def test_arq_worker_settings():
	"""Test arq worker configuration."""
	assert hasattr(WorkerSettings, "functions")
	assert hasattr(WorkerSettings, "redis_settings")

def test_diarization_fallback():
	"""Test speaker diarization fallback output."""
	turns = diarize_audio("dummy.wav")
	assert isinstance(turns, list)
	assert len(turns) >= 1
	assert "speaker" in turns[0]

def test_broll_ai_fallback():
	"""Test AI B-roll fallback output when API key is missing."""
	result = generate_ai_broll("A cinematic sunset", "dummy_broll.mp4")
	assert result is None or isinstance(result, str)
