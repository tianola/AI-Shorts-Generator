"""Background footage for Narrate Studio, found automatically.

The footage comes from Pexels, whose licence allows free use without attribution. We do
not take gameplay or clips from YouTube or other people's channels: they are copyrighted,
and re-using them is what gets a channel flagged for reused content.

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


def available() -> bool:
    return bool(settings.PEXELS_API_KEY)


def fallback_queries(script: str, count: int = 3) -> List[str]:
    """Plain keywords from the script when the model gives nothing: the most frequent long words."""
    words = [w for w in re.findall(r"[a-zA-Z]{5,}", script.lower()) if w not in _STOP]
    ranked = sorted(set(words), key=lambda w: (-words.count(w), words.index(w)))
    return ranked[:count] or ["abstract background"]


async def queries_for(script: str, count: int = 3) -> List[str]:
    """Short visual search phrases that fit the story (what the camera should show)."""
    from services.llm_router import call_llm_json, pick_model

    prompt = (
        f"A narrated short video says:\n{script[:1200]}\n\n"
        f"Give {count} different stock-footage search phrases (2-3 words each) for calm, "
        "visually interesting background video that fits the mood and subject: scenery, "
        "objects, textures or abstract motion. No people's names, no brands, no text on screen. "
        'Return JSON only: {"queries": ["...", "..."]}'
    )
    try:
        raw = await call_llm_json(prompt=prompt, system="You pick stock footage. JSON only.",
                                  model=pick_model(), temperature=0.5, max_tokens=300)
        found = [str(q).strip() for q in json.loads(raw).get("queries", []) if str(q).strip()]
        found = [re.sub(r"[^\w\s-]", "", q)[:40] for q in found]
        if found:
            return found[:count]
    except Exception as exc:
        logger.warning(f"Stock-footage queries fell back to keywords ({type(exc).__name__}: {exc})")
    return fallback_queries(script, count)


def pick_file(video: dict) -> Optional[dict]:
    """The best download of one Pexels video: portrait, no wider than 1080, the sharpest."""
    files = [f for f in video.get("video_files", [])
             if f.get("link") and f.get("width") and f.get("height")
             and str(f.get("file_type", "video/mp4")).endswith("mp4")]
    portrait = [f for f in files if f["height"] >= f["width"] and f["width"] <= 1080]
    pool = portrait or [f for f in files if f["width"] <= 1920]
    return max(pool, key=lambda f: f["width"] * f["height"]) if pool else None


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
            if vid in skip_ids or float(video.get("duration") or 0) < MIN_SECONDS:
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


async def footage_for(script: str, library: Path, count: int = 3) -> List[Path]:
    """Up to `count` different background videos that fit the script. Empty without a key."""
    if not available():
        return []
    import asyncio
    found: List[Path] = []
    used: set = set()
    for query in await queries_for(script, count):
        path = await asyncio.to_thread(fetch, query, library, used)
        if path and path not in found:
            found.append(path)
            used.add(int(re.sub(r"\D", "", path.stem) or 0))
    return found
