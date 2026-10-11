# Contextual Visual Popups & Dark B-Roll Overlay Removal Delivery

```
               [ 00-Index ]
                    │
          ┌─────────┴─────────┐
     [ Summary ]        [ Analysis ]
          │                   │
  01-Exec-Summary       01-Tone-Gating
                        02-Image-Sourcing
                        03-Card-Styling-FFmpeg
                        04-Pipeline-Integration
                              │
                        [ Dossiers ]
                        01-Tone-Detector-Spec
                        02-Image-Fetcher-Spec
                        03-Filtergraph-Spec
                        04-Test-Results
```

## Navigation Index

| Layer | Document | Description |
|---|---|---|
| **Layer 1: Summary** | [01-Executive-Summary](01-summary/01-executive-summary.md) | High-level business overview, outcomes, and metrics |
| **Layer 2: Analysis** | [01-Tone-Gating](02-analysis/01-tone-gating-and-safety.md) | Deterministic safety filter & tone taxonomy gating |
| | [02-Image-Sourcing](02-analysis/02-image-sourcing-and-caching.md) | Tiered image waterfall (Pexels, Wikimedia Commons) & SHA-256 caching |
| | [03-Card-Styling-FFmpeg](02-analysis/03-card-styling-and-filtergraph.md) | Pillow card styling (rounded corners, drop shadow) & spring filtergraph |
| | [04-Pipeline-Integration](02-analysis/04-pipeline-integration-and-verification.md) | Single-pass filter integration, worker wiring & sequential fallback |
| **Layer 3: Dossiers** | [01-Tone-Detector-Spec](03-dossiers/01-tone-detector-spec.md) | Complete module API, regex blocklist, and category dictionary |
| | [02-Image-Fetcher-Spec](03-dossiers/02-image-fetcher-spec.md) | Image downloader, Wikimedia search generator, and cache schema |
| | [03-Filtergraph-Spec](03-dossiers/03-visual-popup-filtergraph-spec.md) | Math formulas for overshoot scale curve and position geometry |
| | [04-Test-Results](03-dossiers/04-test-execution-results.md) | Automated pytest execution log (107/107 tests passing) |

## Quick Summary
- **Obsolete Filter Removal**: Deleted `backend/services/broll.py` and purged all dark-filter overlay calls from `backend/worker.py`.
- **Tone Safety & Gating**: Implemented `backend/services/tone_detector.py` with multi-category classification and regex safety blocklist for somber/tragic narratives.
- **Contextual Image Fetching**: Implemented `backend/services/image_fetcher.py` featuring Pexels + Wikimedia Commons waterfall and disk caching.
- **Animated Visual Cards**: Implemented `backend/services/visual_popup.py` and `backend/services/smart_visuals.py` generating RGBA Pillow cards with drop shadows and FFmpeg spring-animated filtergraph fragments.
- **Pipeline Integration**: Extended `backend/services/render.py` (`build_single_pass_filter` and `render_single_pass`), `backend/services/clip_selector.py`, and `backend/worker.py`.
- **Test Suite**: Created `backend/tests/test_visual_popups.py` (17 tests) — complete test suite of 107 tests passing.
