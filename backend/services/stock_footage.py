"""Background footage for Narrate Studio, found automatically.

The footage comes from Pexels and Pixabay, whose licences allow free use without attribution.
We do not take gameplay or clips from YouTube or other people's channels: they are copyrighted,
and re-using them is what gets a channel flagged for reused content. Pexels is tried first;
Pixabay fills in when Pexels has nothing that fits.

Search words come from the script (one short model call, with a plain keyword fallback),
each hit is downloaded once into storage/narrate/library/auto and reused afterwards.
"""

import json
import re
from pathlib import Path
from typing import List, Optional

import httpx
from loguru import logger

from config import settings

SEARCH_URL = "https://api.pexels.com/videos/search"
MIN_SECONDS = 6
_STOP = set("""about after again against because before being between could does doing during
each from have having here into just more most much only other over same should some such than
that their them then there these they this those through under until very were what when where
which while will with would your""".split())


PIXABAY_URL = "https://pixabay.com/api/videos/"


def available() -> bool:
    return bool(settings.PEXELS_API_KEY or settings.PIXABAY_API_KEY)


def fallback_queries(script: str, count: int = 3) -> List[str]:
    """Plain keywords from the script when the model gives nothing: the most frequent long words."""
    words = [w for w in re.findall(r"[a-zA-Z]{5,}", script.lower()) if w not in _STOP]
    ranked = sorted(set(words), key=lambda w: (-words.count(w), words.index(w)))
    return ranked[:count] or ["abstract background"]


def sentences_of(script: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", script) if len(s.strip()) > 8]


def spread(items: List[str], count: int) -> List[str]:
    """`count` items spread evenly across the list, in order (the whole list when it is short)."""
    if len(items) <= count:
        return items
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)] if count > 1 else items[:1]


# Pleasant, looping footage to sit under the narration: slow motion, light, water, texture.
# Stock search matches words, not story scenes, so for a background the pleasant look matters
# more than a literal match. Each story picks a different handful, deterministically.
AESTHETIC_QUERIES = [
    "slow motion ocean waves", "northern lights timelapse", "soft bokeh city lights night",
    "flowing silk fabric slow motion", "misty mountain sunrise", "water ripples close up",
    "golden hour clouds timelapse", "falling autumn leaves slow motion", "cinematic rain on glass",
    "milky way night sky", "ink drops in water", "candle flame close up",
    "waterfall slow motion", "colorful smoke abstract", "sunlight through forest trees",
    "aerial clouds drifting", "snow falling forest", "neon lights reflection water",
]


async def queries_for(script: str, count: int = 4) -> List[str]:
    """`count` pleasant background searches, chosen per story (the same story gets the same ones)."""
    import random
    import zlib

    picks = random.Random(zlib.crc32(script.encode("utf-8"))).sample(AESTHETIC_QUERIES, count)
    return picks


def pick_file(video: dict) -> Optional[dict]:
    """The best download of one Pexels video: portrait, no wider than 1080, the sharpest."""
    files = [f for f in video.get("video_files", [])
             if f.get("link") and f.get("width") and f.get("height")
             and str(f.get("file_type", "video/mp4")).endswith("mp4")]
    portrait = [f for f in files if f["height"] >= f["width"] and f["width"] <= 1080]
    pool = portrait or [f for f in files if f["width"] <= 1920]
    return max(pool, key=lambda f: f["width"] * f["height"]) if pool else None


def pick_pixabay_file(hit: dict) -> Optional[dict]:
    """The best portrait variant of one Pixabay video (Pixabay's search has no orientation filter)."""
    variants = [v for v in (hit.get("videos") or {}).values()
                if isinstance(v, dict) and v.get("url") and v.get("width") and v.get("height")]
    portrait = [v for v in variants if v["height"] >= v["width"] and v["width"] <= 1080]
    return max(portrait, key=lambda v: v["width"] * v["height"]) if portrait else None


def fetch_pixabay(query: str, library: Path, skip_ids: Optional[set] = None) -> Optional[Path]:
    """Download one portrait Pixabay video for `query`. None when Pixabay has none that fits."""
    key = settings.PIXABAY_API_KEY
    if not key or not query:
        return None
    skip_ids = skip_ids or set()
    try:
        resp = httpx.get(PIXABAY_URL, timeout=15.0,
                         params={"key": key, "q": query, "per_page": 50, "safesearch": "true"})
        if resp.status_code != 200:
            logger.warning(f"Pixabay search '{query}' failed: HTTP {resp.status_code}")
            return None
        for hit in resp.json().get("hits", []):
            vid = hit.get("id")
            if vid is None or f"pixabay_{vid}" in skip_ids or float(hit.get("duration") or 0) < MIN_SECONDS:
                continue
            chosen = pick_pixabay_file(hit)
            if not chosen:
                continue
            target = cache_dir(library) / f"pixabay_{vid}.mp4"
            if target.is_file() and target.stat().st_size > 10_000:
                return target
            with httpx.stream("GET", chosen["url"], timeout=60.0, follow_redirects=True) as dl:
                if dl.status_code != 200:
                    continue
                tmp = target.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    for chunk in dl.iter_bytes(1024 * 256):
                        fh.write(chunk)
            tmp.replace(target)
            return target
    except Exception as exc:
        logger.warning(f"Pixabay fetch '{query}' failed ({type(exc).__name__}: {exc})")
    return None


def cache_dir(library: Path) -> Path:
    path = Path(library) / "auto"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(query: str, library: Path, skip_ids: Optional[set] = None) -> Optional[Path]:
    """Download one fitting video for `query` (or reuse the cached copy). None if there is none."""
    key = settings.PEXELS_API_KEY
    if not key or not query:
        return None
    skip_ids = skip_ids or set()
    try:
        resp = httpx.get(SEARCH_URL, headers={"Authorization": key}, timeout=15.0,
                         params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 15})
        if resp.status_code != 200:
            logger.warning(f"Pexels search '{query}' failed: HTTP {resp.status_code}")
            return None
        for video in resp.json().get("videos", []):
            vid = video.get("id")
            if f"pexels_{vid}" in skip_ids or float(video.get("duration") or 0) < MIN_SECONDS:
                continue
            chosen = pick_file(video)
            if not chosen:
                continue
            target = cache_dir(library) / f"pexels_{vid}.mp4"
            if target.is_file() and target.stat().st_size > 10_000:
                return target
            with httpx.stream("GET", chosen["link"], timeout=60.0, follow_redirects=True) as dl:
                if dl.status_code != 200:
                    continue
                tmp = target.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    for chunk in dl.iter_bytes(1024 * 256):
                        fh.write(chunk)
            tmp.replace(target)
            return target
    except Exception as exc:
        logger.warning(f"Pexels fetch '{query}' failed ({type(exc).__name__}: {exc})")
    return None


async def footage_for(script: str, library: Path, count: int = 4) -> List[Path]:
    """Up to `count` different background videos that fit the script. Empty without a key."""
    if not available():
        return []
    import asyncio
    found: List[Path] = []
    used: set = set()  # cache stems already picked, e.g. "pexels_123"
    for query in await queries_for(script, count):
        path = None
        if settings.PEXELS_API_KEY:
            path = await asyncio.to_thread(fetch, query, library, used)
        if path is None and settings.PIXABAY_API_KEY:
            path = await asyncio.to_thread(fetch_pixabay, query, library, used)
        if path and path not in found:
            found.append(path)
            used.add(path.stem)
    return found
