"""Pexels stock video B-roll integration service."""
import os
import httpx
from typing import Optional
from loguru import logger
from config import settings

def fetch_pexels_broll(query: str, output_path: str) -> Optional[str]:
	"""
	Search and download high-quality stock video B-roll from Pexels API.
	"""
	try:
		pexels_key = os.getenv("PEXELS_API_KEY")
		if not pexels_key:
			return None

		logger.info(f"Searching Pexels for stock B-roll: {query}")
		headers = {"Authorization": pexels_key}
		params = {"query": query, "per_page": 1, "orientation": "portrait", "size": "medium"}

		response = httpx.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=10.0)
		if response.status_code == 200:
			data = response.json()
			videos = data.get("videos", [])
			if videos:
				video_files = videos[0].get("video_files", [])
				# Pick best HD portrait file
				hd_files = [v for v in video_files if v.get("width") and v.get("width") <= 1080]
				if not hd_files:
					hd_files = video_files
				if hd_files:
					file_url = hd_files[0].get("link")
					if file_url:
						r = httpx.get(file_url, timeout=30.0)
						if r.status_code == 200:
							os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
							with open(output_path, "wb") as f:
								f.write(r.content)
							logger.info(f"Successfully downloaded Pexels B-roll to {output_path}")
							return output_path
	except Exception as e:
		logger.warning(f"Pexels B-roll fetch failed: {e}")

	return None
