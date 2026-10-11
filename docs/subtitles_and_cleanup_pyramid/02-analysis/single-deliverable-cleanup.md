# Single Deliverable Cleanup Architecture

## File Lifecycle & Storage Discipline

To prevent disk bloat and ensure deterministic API responses, every clip folder (`storage/jobs/<job_id>/clip_<idx>/`) must contain ONLY the following whitelist:

- `final_video.mp4` (Final composited short with motion title, SFX, and burnt subtitles)
- `thumbnail.jpg` (Extracted high-CTR cover)
- `metadata.json` (Viral copy, hashtags, title, scores)
- `storyboard.jpg` (Visual summary matrix)
- `words.json` (Word-level timestamps for editing)

## Implementation Points

1. **`backend/worker.py`:**
   - Post-rendering moves the resulting file to `final_video.mp4`.
   - Iterates through `clip_dir` and purges any `.mp4`, `.ass`, or `.wav` not in `keep_files`.
   - `_clear_derived_artifacts` also purges intermediate versions on re-runs.

2. **`backend/api/routes.py` (Re-render Endpoint):**
   - Applies the same whitelist cleanup after re-rendering.
