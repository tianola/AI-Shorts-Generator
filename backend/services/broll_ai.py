"""AI B-roll generation service using fal-client or fallback visuals."""
import os
from typing import Optional
from loguru import logger

def generate_ai_broll(prompt: str, output_path: str) -> Optional[str]:
	"""
	Generate an AI B-roll video snippet using fal-client (e.g. Luma/Sora/Hunyuan video models) or fallback.
	"""
	try:
		fal_key = os.getenv("FAL_KEY")
		if fal_key:
			import fal_client
			logger.info(f"Generating AI B-roll via fal_client for prompt: {prompt}")
			handler = fal_client.submit(
				"fal-ai/minimax-video",
				arguments={
					"prompt": prompt,
				},
			)
			result = handler.get()
			video_url = result.get("video", {}).get("url")
			if video_url:
				import httpx
				r = httpx.get(video_url)
				if r.status_code == 200:
					with open(output_path, "wb") as f:
						f.write(r.content)
					return output_path
	except Exception as e:
		logger.warning(f"AI B-roll generation failed or skipped: {e}")

	return None
