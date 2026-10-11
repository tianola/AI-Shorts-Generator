"""
Thin wrapper around the Higgsfield Soul model (higgsfield-ai/soul/v2/standard)
that returns a dict with virality metrics for a local clip file.
If the call fails (no credits, network, etc.) we return None so the caller
can fall back to the existing heuristic scorer.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Dict

from loguru import logger

_client = None


def _get_client():
    """Create the Higgsfield SDK client on first use (picks up HF_KEY from env).

    Imported lazily because the SDK is optional: a module-scope import made
    this file unimportable on any machine without it, for a feature nothing
    calls by default.
    """
    global _client
    if _client is None:
        from higgsfield_client import SyncClient
        _client = SyncClient()
    return _client

def score_clip_virality(clip_path: str) -> Optional[Dict]:
    """
    Upload a local clip to Higgsfield, run the Soul model,
    and return a dict with virality metrics.

    Returns None on any failure (so the caller can use the heuristic fallback).
    """
    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    try:
        client = _get_client()

        # ---- 1. Upload the clip (returns a signed, temporary URL) ----
        upload_resp = client.upload_file(clip_path)
        video_url = upload_resp.get("url")
        if not video_url:
            raise RuntimeError("Upload succeeded but no URL returned")

        # ---- 2. Submit Soul model request ----
        submit_resp = client.submit(
            "higgsfield-ai/soul/v2/standard",
            {"input": {"video_url": video_url}},
        )
        request_id = submit_resp.id

        # ---- 3. Poll until terminal state (simple loop - fine for low volume) ----
        while True:
            status = client.status(request_id)
            if status in ("Completed", "Failed", "Cancelled"):
                break
            time.sleep(2)   # adjust if you want faster/slower polling

        # ---- 4. Fetch result ----
        result = client.result(request_id)

        # ---- 5. Extract the fields we care about ----
        out: Dict = {
            "virality_score": getattr(result, "virality_score", None),
            "peak_hook_timestamp": getattr(result, "peak_hook_timestamp", None),
            "hold_rate": getattr(result, "hold_rate", None),
        }
        # If the model didn't give us a virality_score, treat as failure.
        if out["virality_score"] is None:
            return None
        return out

    except Exception as e:
        # Never bubble up - the caller falls back to the heuristic scorer.
        logger.warning(f"[Higgsfield Virality] Error scoring {clip_path}: {e}")
        return None