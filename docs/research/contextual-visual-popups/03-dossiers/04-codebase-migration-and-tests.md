# Layer 3: Dossier — 04: Codebase Migration Inventory, Diffs, and Test Harness

## 1. File Modification & Deletion Inventory

| File | Action | Description |
|:---|:---:|:---|
| `backend/services/broll.py` | **DELETE** | Remove obsolete dark filter overlay (`eq=contrast...`). |
| `backend/services/tone_detector.py` | **CREATE** | Regex safety blocklist + tone categorization helpers. |
| `backend/services/image_fetcher.py` | **CREATE** | Waterfall image search (Pexels, Wikimedia) + SHA-256 disk cache. |
| `backend/services/visual_popup.py` | **CREATE** | Pillow card generator (rounded corners + drop shadow) + filter builders. |
| `backend/services/render.py` | **MODIFY** | Add `popup_png`, `popup_start`, `popup_duration` parameters to `build_single_pass_filter`. |
| `backend/services/clip_selector.py` | **MODIFY** | Upgrade LLM prompt to include tone taxonomy and `popup_cues`. |
| `backend/worker.py` | **MODIFY** | Replace legacy broll step with contextual popup preparation. |
| `backend/tests/test_visual_popups.py` | **CREATE** | Comprehensive test suite for tone detection, card rendering, and filter assembly. |

---

## 2. Code Changes / Integration Blueprint

### 2.1 `backend/worker.py` (Step 5e Replacement)

**Legacy Code (to remove)**:
```python
# Step 5e: Contextual Visuals / Image Popups (B-Roll)
broll_query = clip.get("broll_query")
if broll_query:
    try:
        from services.broll_pexels import fetch_and_overlay_broll
        broll_out = os.path.join(str(clip_dir), "with_broll.mp4")
        b_start = float(clip.get("broll_start", 2.0))
        b_dur = float(clip.get("broll_duration", 3.5))
        res_broll = fetch_and_overlay_broll(final_path, broll_out, broll_query, start_time=b_start, duration=b_dur)
        if res_broll:
            final_path = res_broll
    ...
```

**Modern Single-Pass Integration (to implement)**:
In the single-pass render stage (`services/render.py` / `worker.py`):
```python
# In worker.py before render_single_pass:
popup_png = None
popup_start = None
popup_duration = 2.0

popup_cues = clip.get("popup_cues", [])
if clip.get("popup_eligible", True) and popup_cues:
    cue = popup_cues[0]
    query = cue.get("query")
    if query:
        from services.image_fetcher import get_contextual_image
        from services.visual_popup import create_styled_popup_card
        
        raw_img = await get_contextual_image(query)
        if raw_img:
            card_path = os.path.join(str(clip_dir), "popup_card.png")
            popup_png = create_styled_popup_card(raw_img, card_path)
            popup_start = float(cue.get("start_offset", 3.0))
            popup_duration = float(cue.get("duration", 2.0))
```

---

## 3. Unit Test Suite: `backend/tests/test_visual_popups.py`

```python
import os
import pytest
from unittest.mock import patch, AsyncMock
from services.tone_detector import is_transcript_somber, classify_content_tone
from services.visual_popup import create_styled_popup_card, build_popup_filtergraph_fragment
from services.render import build_single_pass_filter

def test_tone_safety_blocklist():
    # Negative / Somber transcripts must be blocked
    assert is_transcript_somber("He sadly died in the tragic accident") is True
    assert is_transcript_somber("The patient is in intensive care with cancer") is True
    assert is_transcript_somber("Victims of the devastating earthquake") is True
    
    # Casual / Energetic transcripts must pass
    assert is_transcript_somber("This new AI tool builds full apps in seconds") is False
    assert is_transcript_somber("Here is the secret to 10x your productivity") is False

def test_popup_filtergraph_math():
    prep, anim_label, overlay = build_popup_filtergraph_fragment(
        input_idx=2,
        start_time=3.5,
        duration=2.0,
        position="upper_third"
    )
    assert "[2:v]format=rgba" in prep
    assert "scale=w=" in prep
    assert "eval=frame" in prep
    assert "between(t\\,3.5\\,5.5)" in overlay
    assert "overlay=x='(W-w)/2'" in overlay

def test_single_pass_filter_with_popup():
    extra, fc, vmap, amap = build_single_pass_filter(
        vertical_frag="[0:v]crop=607:1080:656:0,scale=1080:1920[vsingle]",
        popup_png="/tmp/dummy_popup.png",
        popup_start=2.5,
        popup_duration=2.0
    )
    assert len(extra) == 1
    assert extra[0]["path"] == "/tmp/dummy_popup.png"
    assert extra[0]["kind"] == "popup"
    assert "popup_anim" in fc
    assert "between(t" in fc
```

---

## SOURCES
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\worker.py` -> Worker execution pipeline.
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\services\render.py` -> Single-pass rendering engine.
