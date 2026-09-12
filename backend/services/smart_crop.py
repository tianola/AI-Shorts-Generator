"""Smart-crop wrapper for Shorts-style face tracking."""
from typing import Optional

from services.vertical import make_vertical, make_vertical_static


def smart_crop_video(
	input_file: str,
	output_file: str,
	width: int = 1080,
	height: int = 1920,
	mode: Optional[str] = None,
) -> str:
	"""Apply the configured vertical crop mode.

	This keeps the project aligned with the AI Shorts architecture: face tracking
	is the default, with static crop as a safe fallback.
	"""
	if mode is None:
		mode = "face_tracked"

	if mode == "face_tracked":
		return make_vertical(input_file, output_file, width, height)
	if mode == "static":
		return make_vertical_static(input_file, output_file, width, height)

	return make_vertical(input_file, output_file, width, height)
