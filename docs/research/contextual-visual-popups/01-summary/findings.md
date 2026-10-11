# Layer 1: Executive Summary & System Findings

## 1. Research Objectives & Problem Statement
The legacy B-Roll implementation in `backend/services/broll.py` applied a destructive, full-duration dark filter and vignette (`eq=contrast=1.04:saturation=1.08,vignette=PI/4`) that darkened the entire video without adding relevant visual value. In modern short-form vertical video (TikTok, YouTube Shorts, Instagram Reels), viewer engagement relies on dynamic visual reinforcement — contextual popups (reaction memes, relevant stock photos, charts, and brand/entity badges) that appear at key punchlines or subject changes.

However, displaying humorous or meme popups during serious, tragic, or somber content causes catastrophic tone dissonance. This research delivers a production-ready blueprint for an intelligent, tone-gated contextual visual popup system.

---

## 2. Key Findings & Architectural Verdicts

### Finding 1: Two-Stage Tone & Context Gating (Task 1)
- **Verdict**: Tone gating must execute via a **Hybrid Two-Stage Pipeline**:
  1. *Deterministic Safety Guard (Stage 0)*: Zero-latency regex scanning of transcripts against a curated blocklist of 80+ somber/tragic categories (violence, death, disaster, medical emergencies, legal proceedings). If matched, popups are instantly disabled (`popup_eligible = False`) without LLM overhead.
  2. *Semantic Vibe Classification (Stage 1)*: Integrated into the existing `clip_selector.py` LLM call to classify video vibe into 3 primary gating classes:
     - `ALLOWED_ENERGETIC` (Comedy, casual podcast, gaming, reaction, energetic vlog) -> Full meme and stock image popups enabled.
     - `ALLOWED_MINIMAL` (Educational explainer, tech review, business case) -> Clean, subtle informational graphics and photos only (no meme/reaction stickers).
     - `SUPPRESSED_SERIOUS` (Documentary, news reporting, somber, tragic) -> Popups strictly disabled (0 overlays).
- **Timing & Density Rules**: Maximum 1 popup per 20 seconds of clip duration (typical 1-2 popups for a 30-50s Short). Popups are forbidden during the initial 1.5s (title card entrance) and the final 2.0s (call-to-action outro).

### Finding 2: Tiered Multi-Source Image Retrieval & Disk Caching (Task 2)
- **Verdict**: Image sourcing must follow a 4-tier waterfall fallback to guarantee 100% availability with sub-50ms latency for frequent assets:
  - **Tier 0 (Local Curated Assets)**: 50+ pre-bundled high-resolution reaction stickers, emojis, and meme templates stored locally in `backend/assets/visuals/`. Zero network calls, zero failure rate.
  - **Tier 1 (Pexels Image API)**: Leverages the existing `PEXELS_API_KEY` configuration to search high-resolution photos with orientation and dimension filtering.
  - **Tier 2 (Wikimedia Commons API)**: Keyless, zero-authentication public domain discovery for named entities, historical events, flags, logos, and scientific concepts.
  - **Tier 3 (Unsplash API Fallback)**: Optional high-aesthetic editorial photography for abstract or lifestyle queries.
- **Persistent Caching Engine**: SHA-256 hashed on-disk storage under `storage/cache/images/{hash}.png` with an in-memory LRU index to prevent redundant network downloads and respect API rate limits.

### Finding 3: Non-Intrusive Geometry & Spring-Animated Popups (Task 3)
- **Verdict**: Direct raw video overlays are intrusive and obscure faces or captions. The system must use a **Two-Step Hybrid Compositor**:
  1. *PIL Card Pre-Processing (Software Stage)*: Raw fetched images are dynamically cropped, given rounded corners (radius 24px), a clean 4px white/neon border, and a soft Gaussian drop shadow (radius 16px, alpha 0.45) on a transparent RGBA canvas.
  2. *FFmpeg Spring Scale-Pop Animation (Hardware Stage)*: Entrance animation uses a mathematical spring overshoot expression (`eval=frame`): grows from 0.85x to 1.12x at `t=0.18s`, settles to 1.0x at `t=0.28s`, maintains steady hold, and smoothly fades out over the last 0.20s.
  3. *Safe-Zone Placement*: Fixed in the **Upper-Third Region** (`x=(W-w)/2`, `y=280` to `340` on a 1080x1920 canvas) or **Upper-Right Badge** (`x=W-w-60`, `y=320`), keeping the title zone (`y=0-240`) and caption zone (`y=1250-1750`) completely unobstructed.

### Finding 4: Single-Pass Unified Pipeline Integration (Task 4)
- **Verdict**: `backend/services/broll.py` must be deleted. Visual popups must be composited inside `backend/services/render.py` (`build_single_pass_filter`) as Stage 3.5.
- **Performance Impact**: Compositing popups within the existing single-pass filtergraph adds **0 additional video re-encodes**, preserving 100% audio fidelity and keeping render times under 4.5 seconds on GPU / 12 seconds on CPU.

---

## 3. Priority Implementation Roadmap

| Step | Action Item | Target File | Impact |
|:---|:---|:---|:---|
| **1** | Delete obsolete dark filter overlay | `backend/services/broll.py` | Removes destructive video darkening |
| **2** | Implement tone gating & safety blocklist | `backend/services/tone_detector.py` | Prevents offensive or tone-deaf popups |
| **3** | Implement tiered image fetcher + cache | `backend/services/image_fetcher.py` | Fast, resilient image sourcing |
| **4** | Implement PIL card generator & styling | `backend/services/visual_popup.py` | Drop-shadowed, rounded cards |
| **5** | Integrate popup inputs into single-pass FFmpeg | `backend/services/render.py` | Zero-re-encode animated overlay |
| **6** | Wire clip selector & worker lifecycle | `backend/services/clip_selector.py`, `backend/worker.py` | Autonomous end-to-end clip generation |

---

## SOURCES (LAYER 2 NAVIGATION)
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\01-tone-context-gating.md
 -> Detailed tone classification taxonomy, safety heuristics, prompt schemas, and timing density rules.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\02-contextual-image-sourcing.md
 -> Full API comparison (Pexels, Wikimedia, Unsplash), local curated asset matrix, and SHA-256 caching architecture.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\03-animated-popup-spec.md
 -> Frame-accurate FFmpeg animation expressions, mathematical spring curves, PIL card styling, and safe-zone geometry.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\04-system-api-architecture.md
 -> End-to-end service interaction diagram, single-pass filter graph assembly, API contracts, and worker changes.
