"""
Thin wrapper around the Higgsfield Soul model (higgsfield-ai/soul/v2/standard)
that returns a dict with virality metrics for a local clip file.
If the call fails (no credits, network, etc.) we return None so the caller
can fall back to the existing heuristic scorer.
"""
from __future__ import annotations

import os
from typing import Optional, Dict

from higgsfield_client import SyncClient, HiggsfieldClientError

# The SDK automatically picks up HF_KEY from the environment.
_client = SyncClient()   # type: ignore[var-annotated]

def score_clip_virality(clip_path: str) -> Optional[Dict]:
    """
    Upload a local clip to Higgsfield, run the Soul model,
    and return a dict with virality metrics.

    Returns None on any failure (so the caller can use the heuristic fallback).
    """
    if not os.path.isfile(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    try:
        # ---- 1️⃣ Upload the clip (returns a signed, temporary URL) ----
        upload_resp = _client.upload_file(clip_path)
        video_url = upload_resp.get("url")
        if not video_url:
            raise HiggsfieldClientError("Upload succeeded but no URL returned")

        # ---- 2️⃣ Submit Soul model request ----
        submit_resp = _client.submit(
            "higgsfield-ai/soul/v2/standard",
            {"input": {"video_url": video_url}},
        )
        request_id = submit_resp.id

        # ---- 3️⃣ Poll until terminal state (simple loop – fine for low volume) ----
        import time
        while True:
            status = _client.status(request_id)
            if status in ("Completed", "Failed", "Cancelled"):
                break
            time.sleep(2)   # adjust if you want faster/slower polling

        # ---- 4️⃣ Fetch result ----
        result = _client.result(request_id)

        # ---- 5️⃣ Extract the fields we care about ----
        out: Dict = {
            "virality_score": getattr(result, "virality_score", None),
            "peak_hook_timestamp": getattr(result, "peak_hook_timestamp", None),
            "hold_rate": getattr(result, "hold_rate", None),
            # you could also pull `brain_heatmap` if you need it later
        }
        # If the model didn't give us a virality_score, treat as failure.
        if out["virality_score"] is None:
            return None
        return out

    except HiggsfieldClientError as e:
        # Log but do not bubble up – let the caller decide what to do.
        # You could also inspect e.status_code for 403/429 etc.
        print(f"[Higgsfield Virality] Error scoring {clip_path}: {e}")
        return None