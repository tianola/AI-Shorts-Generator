# Research & Design: Contextual Visual Popups & Tone Detection for B-Roll

**Project**: AI Shorts Generator (`E:\01-AI-PROJECTS\ai-shorts-generator`)  
**Task ID**: `t_0fb44525`  
**Assignee**: Researcher  
**Specification Version**: 1.0.0 (Production Blueprint)  
**Date**: September 2026  

---

## 1. Document Overview & Orientation
This artifact pyramid provides the complete architectural research, design specification, mathematical filter definitions, and implementation blueprint to replace obsolete dark filter overlays (`backend/services/broll.py`) with an intelligent, tone-gated contextual visual pop-up system (images, reaction memes, encyclopedic graphics, and thematic badges).

---

## 2. Progressive Disclosure Navigation

```
                                ┌───────────────────────────┐
                                │     00-index.md (Root)    │
                                └─────────────┬─────────────┘
                                              │
                                ┌─────────────▼─────────────┐
                                │  01-summary/findings.md   │
                                └─────────────┬─────────────┘
                                              │
            ┌───────────────────┬─────────────┴─────┬───────────────────┐
            │                   │                   │                   │
  ┌─────────▼─────────┐ ┌───────▼─────────┐ ┌───────▼─────────┐ ┌───────▼─────────┐
  │   02-analysis/    │ │   02-analysis/    │ │   02-analysis/    │ │   02-analysis/    │
  │01-tone-gating.md  │ │02-image-source.md │ │03-popup-spec.md   │ │04-architecture.md │
  └─────────┬─────────┘ └───────┬─────────┘ └───────┬─────────┘ └───────┬─────────┘
            │                   │                   │                   │
  ┌─────────▼─────────┐ ┌───────▼─────────┐ ┌───────▼─────────┐ ┌───────▼─────────┐
  │   03-dossiers/    │ │   03-dossiers/    │ │   03-dossiers/    │ │   03-dossiers/    │
  │01-tone-prompts.md │ │02-providers-cache │ │03-ffmpeg-filters  │ │04-migration-tests │
  └───────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘
```

### Layer 1: Executive Summary & Decisions
- **[01-summary/findings.md](01-summary/findings.md)** — Architectural verdicts, core findings across the 4 research questions, system implications, and delivery roadmap.

### Layer 2: Analysis Collection (Per-Dimension Technical Specs)
- **[02-analysis/01-tone-context-gating.md](02-analysis/01-tone-context-gating.md)** — Two-stage tone and sentiment gating framework (LLM classification + rule-based safety suppressors) to prevent inappropriate visuals.
- **[02-analysis/02-contextual-image-sourcing.md](02-analysis/02-contextual-image-sourcing.md)** — Tiered image retrieval architecture: Tier 0 Local Assets, Tier 1 Pexels Image API, Tier 2 Wikimedia Commons (keyless), Tier 3 Unsplash, backed by SHA-256 disk caching.
- **[02-analysis/03-animated-popup-spec.md](02-analysis/03-animated-popup-spec.md)** — Frame-accurate FFmpeg filtergraph specifications, smooth scale-pop spring curves, PIL card pre-compositing (drop shadow + rounded borders), and vertical safe zone geometry.
- **[02-analysis/04-system-api-architecture.md](02-analysis/04-system-api-architecture.md)** — End-to-end pipeline redesign, removal of `services/broll.py`, unified single-pass filter integration (`render_single_pass`), API data contracts, and worker lifecycle.

### Layer 3: Dossiers & Verification Material
- **[03-dossiers/01-tone-prompts-and-classification.md](03-dossiers/01-tone-prompts-and-classification.md)** — Exact LLM system prompts, JSON validation schemas, hard negative keyword blocklists, and edge-case classification matrices.
- **[03-dossiers/02-image-providers-and-cache-benchmarks.md](03-dossiers/02-image-providers-and-cache-benchmarks.md)** — API request formats, network latency benchmarks, LRU/SHA-256 cache implementation, and Pillow card renderer code.
- **[03-dossiers/03-ffmpeg-popup-filtergraphs.md](03-dossiers/03-ffmpeg-popup-filtergraphs.md)** — Evaluated FFmpeg expressions, trigonometric overshoot formulas, filter complex chains, and standalone CLI test recipes.
- **[03-dossiers/04-codebase-migration-and-tests.md](03-dossiers/04-codebase-migration-and-tests.md)** — Migration inventory, diff plan for `worker.py`, `clip_selector.py`, and `render.py`, plus full pytest test suite specifications.

---

## 3. Provenance & Metadata
- **Repository Root**: `E:\01-AI-PROJECTS\ai-shorts-generator`
- **Output Artifact Location**: `E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\`
- **Target Components**: `backend/services/visual_popup.py`, `backend/services/tone_detector.py`, `backend/services/image_fetcher.py`, `backend/services/render.py`, `backend/services/clip_selector.py`, `backend/worker.py`
- **Obsolete Components Flagged for Removal**: `backend/services/broll.py`

---

## SOURCES (LAYER 1 & 2 NAVIGATION)
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\01-summary\findings.md
 -> Executive findings, architectural verdicts, and system implications across all 4 research questions.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\01-tone-context-gating.md
 -> Tone classification taxonomy, heuristic safety suppressors, and timing density rules.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\02-contextual-image-sourcing.md
 -> Multi-provider image discovery, curated local asset pack, and SHA-256 caching architecture.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\03-animated-popup-spec.md
 -> FFmpeg filtergraph animation mathematics, PIL card styling, and safe-zone screen layout.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\04-system-api-architecture.md
 -> Modular service architecture, single-pass render integration, API schema, and worker flow.
