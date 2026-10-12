"""
Contextual Image Fetching & Sourcing Service with Tiered Waterfall and SHA-256 Disk Cache.
Tier 0: Local Curated Asset Pack
Tier 1: Pexels Image API (Stock Photos)
Tier 2: Wikimedia Commons API (Keyless Public Domain / Named Entities / Logos)
Tier 3: Unsplash / Local Fallback
"""
import os
import hashlib
import asyncio
from typing import Optional
from PIL import Image
import httpx
from loguru import logger
from config import settings

def _get_cache_dir() -> str:
    cache_dir = os.path.join(settings.STORAGE_ROOT, "cache", "images")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _get_cache_path(query: str) -> str:
    h = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()
    return os.path.join(_get_cache_dir(), f"{h}.png")


def check_local_curated_assets(query: str) -> Optional[str]:
    """
    Tier 0: Check if query matches local curated memes/assets.
    """
    clean_q = query.strip().lower().replace(" ", "_")
    search_dirs = [
        os.path.join(settings.STORAGE_ROOT, "assets", "memes"),
        os.path.join(settings.STORAGE_ROOT, "assets", "images"),
        os.path.join(os.path.dirname(__file__), "..", "assets", "memes"),
        os.path.join(os.path.dirname(__file__), "..", "assets", "images"),
    ]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for ext in [".png", ".jpg", ".jpeg", ".webp"]:
            candidate = os.path.join(d, f"{clean_q}{ext}")
            if os.path.isfile(candidate):
                return candidate
            # Check prefix / substring matches
            for fname in os.listdir(d):
                if fname.lower().startswith(clean_q) and fname.lower().endswith(ext):
                    return os.path.join(d, fname)
    return None


async def fetch_image_from_pexels(query: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Tier 1: High-res stock photography via Pexels Image Search API.
    """
    key = os.getenv("PEXELS_API_KEY") or getattr(settings, "PEXELS_API_KEY", None)
    if not key:
        return None
    try:
        headers = {"Authorization": key}
        params = {"query": query, "per_page": 3, "orientation": "landscape"}
        r = await client.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=5.0)
        if r.status_code == 200:
            photos = r.json().get("photos", [])
            if photos:
                src = photos[0].get("src", {})
                return src.get("medium") or src.get("large") or src.get("original")
    except Exception as e:
        logger.debug(f"Pexels image search failed for '{query}': {e}")
    return None


async def fetch_image_from_wikimedia(query: str, client: httpx.AsyncClient) -> Optional[str]:
    """
    Tier 2: Keyless Wikimedia Commons REST API for named entities, logos, history, science.
    """
    try:
        url = "https://commons.wikimedia.org/w/api.php"
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": "6",
            "gsrlimit": "3",
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "format": "json",
        }
        headers = {"User-Agent": "AIShortsGenerator/2.0 (contact@aishorts.local)"}
        r = await client.get(url, params=params, headers=headers, timeout=5.0)
        if r.status_code == 200:
            pages = r.json().get("query", {}).get("pages", {})
            for _, info in pages.items():
                img_list = info.get("imageinfo", [])
                if not img_list:
                    continue
                img_info = img_list[0]
                mime = img_info.get("mime", "")
                if mime in ["image/jpeg", "image/png", "image/webp"] and img_info.get("width", 0) >= 200:
                    return img_info.get("url")
    except Exception as e:
        logger.debug(f"Wikimedia fetch failed for '{query}': {e}")
    return None


async def get_contextual_image(query: str) -> Optional[str]:
    """
    Retrieve contextual image via waterfall (Local -> Cache -> Pexels -> Wikimedia),
    storing normalized PNG into the SHA-256 disk cache.
    
    Returns:
        Local filesystem path to valid PNG image, or None.
    """
    if not query or not query.strip():
        return None

    # Tier 0: Local Asset Pack
    local_match = check_local_curated_assets(query)
    if local_match:
        logger.info(f"Image Sourcing: Matched Tier 0 local asset for '{query}' -> {local_match}")
        return local_match

    # Disk Cache Check
    cache_path = _get_cache_path(query)
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 100:
        logger.debug(f"Image Sourcing: Cache hit for '{query}' -> {cache_path}")
        return cache_path

    # Tier 1 & Tier 2 Remote Waterfall
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
        # Tier 1: Pexels
        img_url = await fetch_image_from_pexels(query, client)

        # Tier 2: Wikimedia Commons
        if not img_url:
            img_url = await fetch_image_from_wikimedia(query, client)

        if not img_url:
            logger.debug(f"Image Sourcing: No remote image found across waterfall for '{query}'")
            return None

        # Download and normalize image into cache
        try:
            resp = await client.get(img_url)
            if resp.status_code == 200 and len(resp.content) > 100:
                temp_raw = cache_path + ".tmp"
                with open(temp_raw, "wb") as f:
                    f.write(resp.content)

                # Validate and convert with Pillow
                with Image.open(temp_raw) as img:
                    img.convert("RGBA").save(cache_path, "PNG")

                if os.path.exists(temp_raw):
                    try:
                        os.remove(temp_raw)
                    except OSError:
                        pass

                logger.info(f"Image Sourcing: Cached new image for '{query}' -> {cache_path}")
                return cache_path
        except Exception as e:
            logger.warning(f"Failed to download/save image from {img_url}: {e}")

    return None


def get_contextual_image_sync(query: str) -> Optional[str]:
    """Synchronous helper for get_contextual_image."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, get_contextual_image(query)).result()
        else:
            return loop.run_until_complete(get_contextual_image(query))
    except Exception:
        return asyncio.run(get_contextual_image(query))
