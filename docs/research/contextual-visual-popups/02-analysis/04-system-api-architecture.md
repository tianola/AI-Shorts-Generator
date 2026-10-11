# Layer 2: Analysis — Dimension 4: System Architecture & API Design

## 1. System Topology & Architectural Redesign

The legacy architecture contained disconnected B-roll services (`broll.py`, `broll_pexels.py`, `broll_ai.py`) that performed sequential video overlays after main rendering, resulting in unnecessary transcoding steps, potential audio sync drift, and visual darkening.

The modern architecture unifies tone detection, image fetching, card rendering, and video compositing into a cohesive, single-pass pipeline.

```
                          ┌─────────────────────────────┐
                          │   Video Audio & Transcript  │
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │     services/whisper.py     │
                          │   (Word-Level Timestamps)   │
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │ services/clip_selector.py   │
                          │ • Extract Viral Moments     │
                          │ • Tone & Vibe Gating        │
                          │ • Visual Context Cues       │
                          └──────────────┬──────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    ▼                                         ▼
      [ If popup_eligible == True ]             [ If popup_eligible == False ]
                    │                                         │
                    ▼                                         │
     ┌─────────────────────────────┐                          │
     │ services/image_fetcher.py   │                          │
     │ • Tier 0 Local Asset Pack   │                          │
     │ • Tier 1 Pexels Image API   │                          │
     │ • Tier 2 Wikimedia Commons  │                          │
     │ • SHA-256 Disk Cache        │                          │
     └──────────────┬──────────────┘                          │
                    │ (raw PNG/JPG)                           │
                    ▼                                         │
     ┌─────────────────────────────┐                          │
     │ services/visual_popup.py    │                          │
     │ • Pillow Card Compositor    │                          │
     │ • Rounded Corners & Borders │                          │
     │ • Soft Gaussian Drop Shadow │                          │
     └──────────────┬──────────────┘                          │
                    │ (prepared popup_card.png)               │
                    ▼                                         ▼
     ┌─────────────────────────────────────────────────────────────┐
     │ services/render.py  (build_single_pass_filter)              │
     │ Stage 0: Smart Blur Protection (Unsafe objects)             │
     │ Stage 1: Face-Tracked Vertical Crop (1080x1920)             │
     │ Stage 2: Subtitle Burn (ASS / Karaoke)                      │
     │ Stage 3: Motion-Graphic Title Card PNG                      │
     │ Stage 3.5: Contextual Visual Popup (Scale-Pop Animation)    │
     │ Stage 4: SFX Audio Mixing (100% Voice Preserved)            │
     └──────────────────────────────┬──────────────────────────────┘
                                    │ (Unified Single FFmpeg Pass)
                                    ▼
                         [ final_short.mp4 ]
```

---

## 2. Component Decommissioning & New Modules

### 2.1 Decommissioning `backend/services/broll.py`
- **Action**: Completely delete `backend/services/broll.py`.
- **Reason**: Contains only obsolete full-frame vignette and color grade commands (`eq=contrast=1.04...`) which degrade output quality and cause unnecessary re-encoding passes.

### 2.2 New Core Services
1. **`backend/services/tone_detector.py`**:
   - `detect_clip_tone(transcript: str) -> ToneGatingResult`
   - Scans negative safety blocklist and classifies content into tone taxonomy.
2. **`backend/services/image_fetcher.py`**:
   - `async fetch_context_image(query: str, category: str = "general") -> Optional[str]`
   - Manages Tier 0-3 retrieval waterfall and disk caching under `storage/cache/images/`.
3. **`backend/services/visual_popup.py`**:
   - `prepare_popup_card(image_path: str, output_card_path: str, style: str = "card") -> str`
   - Generates transparent PNG card with rounded corners (r=24px), border (4px), and drop shadow.
   - `build_popup_filter_fragment(...) -> tuple[list[dict], str]`

---

## 3. Single-Pass Pipeline Integration (`backend/services/render.py`)

The function `build_single_pass_filter` is enhanced with optional popup arguments:
```python
def build_single_pass_filter(
    vertical_frag: str,
    captions_path: str = None,
    title_png: str = None,
    sfx_files: list = None,
    sfx_timestamps: list = None,
    has_audio: bool = True,
    smart_blur_frag: str = None,
    popup_png: str = None,          # <--- NEW: Path to styled card PNG
    popup_start: float = None,       # <--- NEW: Timestamp in seconds
    popup_duration: float = None,    # <--- NEW: Duration in seconds (default 2.0)
    popup_position: str = "upper_third" # <--- NEW: "upper_third" | "upper_right"
) -> tuple:
```

### Execution Flow in `render.py`:
When `popup_png` is provided:
1. It is appended to `extra_inputs` as an image stream with `-loop 1`.
2. A dedicated animation node `[popup_anim]` is created using the spring scale expression and alpha fade.
3. It is overlaid onto the video stream at `cur_v` with an `enable='between(t, popup_start, popup_start+popup_duration)'` gate.
4. Video rendering executes in a **single FFmpeg process** with CRF 20 and preset `veryfast`.

---

## 4. Clip Selection Schema & Metadata Contract

The LLM prompt in `backend/services/clip_selector.py` emits the updated schema:
```json
{
  "clips": [
    {
      "start": 12.4,
      "end": 44.8,
      "title": "THE AI REVOLUTION",
      "hook": "Why everyone is wrong about AI models",
      "virality_score": 0.94,
      "tone_category": "casual_tech",
      "popup_eligible": true,
      "popup_cues": [
        {
          "query": "supercomputer",
          "start_offset": 5.2,
          "duration": 2.2,
          "type": "stock_minimal",
          "position": "upper_third"
        }
      ]
    }
  ]
}
```

---

## SOURCES (LAYER 3 NAVIGATION)
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\03-dossiers\04-codebase-migration-and-tests.md
 -> Concrete migration diffs for `worker.py`, `render.py`, and comprehensive test fixtures.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\03-dossiers\03-ffmpeg-popup-filtergraphs.md
 -> Direct FFmpeg string assembly and filtergraph integration code.
