"""Speaker diarization service using pyannote.audio or fallback heuristics."""
import os
from typing import List, Dict, Any
from loguru import logger

def diarize_audio(audio_path: str) -> List[Dict[str, Any]]:
	"""
	Perform speaker diarization on audio file.
	Returns list of speaker turns: [{'speaker': 'SPEAKER_00', 'start': 0.0, 'end': 4.5}, ...]
	"""
	try:
		# If pyannote.audio model is available / token is set, use it.
		# Otherwise fallback to a single default speaker or heuristic split.
		hf_token = os.getenv("HUGGINGFACE_TOKEN")
		if hf_token:
			from pyannote.audio import Pipeline
			pipeline = Pipeline.from_pretrained(
				"pyannote/speaker-diarization-3.1",
				use_auth_token=hf_token
			)
			diarization = pipeline(audio_path)
			turns = []
			for turn, _, speaker in diarization.itertracks(yield_label=True):
				turns.append({
					"speaker": speaker,
					"start": turn.start,
					"end": turn.end
				})
			return turns
	except Exception as e:
		logger.warning(f"Pyannote diarization skipped or failed ({e}), using default speaker.")

	return [{"speaker": "SPEAKER_00", "start": 0.0, "end": 9999.0}]
