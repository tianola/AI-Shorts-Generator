"""Audio polish utilities for cleaner short-form narration."""
import os
import re
import subprocess


_FILLER_WORDS = {
	"uh",
	"um",
	"uhm",
	"like",
	"you know",
	"basically",
	"literally",
	"actually",
}


def remove_filler_words(text: str) -> str:
	"""Strip very common filler words from transcript text."""
	if not text:
		return ""
	cleaned = text
	for filler in sorted(_FILLER_WORDS, key=len, reverse=True):
		cleaned = re.sub(rf"\b{re.escape(filler)}\b", "", cleaned, flags=re.IGNORECASE)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	return cleaned


def polish_audio(input_audio: str, output_audio: str) -> str:
	"""Run lightweight audio cleanup.

	This creates a cleaner voice track without requiring heavy denoising models.
	"""
	os.makedirs(os.path.dirname(output_audio) if os.path.dirname(output_audio) else ".", exist_ok=True)

	cmd = [
		"ffmpeg",
		"-y",
		"-i", input_audio,
		"-af",
		"silenceremove=1:0:-50dB,highpass=f=80,lowpass=f=8000,dynaudnorm=p=0.9:s=5,volume=1.2",
		"-ar", "16000",
		output_audio,
	]

	subprocess.run(cmd, check=True, capture_output=True)
	return output_audio
