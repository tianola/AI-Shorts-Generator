# Subtitle Phrasing, Isolated Pop & Deliverable Cleanup — Artifact Pyramid

Welcome to the architectural artifact pyramid for the dynamic 4-5 word subtitle phrasing engine, isolated per-word pop karaoke system (zero sentence jitter), and aggressive single-deliverable video cleanup pipeline.

## Structure

- **L1 Summary:** `01-summary/executive-summary.md` — Key architectural decisions, performance metrics, and deliverable specifications.
- **L2 Analysis:** `02-analysis/`
  - `dynamic-phrasing-architecture.md` — 4-5 word dynamic phrasing algorithm, character caps (24-32 chars), punctuation heuristics, and pause triggers.
  - `isolated-pop-karaoke-engine.md` — ASS dialogue generation with vertical-only scale pop, steady sentence text layout, and zero horizontal jitter.
  - `single-deliverable-cleanup.md` — Single deliverable lifecycle (`final_video.mp4`), intermediate file removal rules in `worker.py` and `routes.py`.
- **L3 Dossiers:** `03-dossiers/`
  - `verification-and-test-suite.md` — Pytest verification results, test fixtures, and validation test cases.

## Sources & References

- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\services\captions.py`
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\worker.py`
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\api\routes.py`
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\tests\test_pro_captions.py`
