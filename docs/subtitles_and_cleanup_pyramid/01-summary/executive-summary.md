# L1 Executive Summary: Subtitles & Single Deliverable System

## Overview

The AI Shorts Generator captioning engine has been upgraded to deliver high-converting, TikTok/OpusClip-grade dynamic subtitles with zero sentence jitter, paired with strict storage discipline that enforces a single final deliverable (`final_video.mp4`).

## Key Enhancements

1. **Dynamic 4-5 Word Phrasing (`_group_words_by_natural_phrasing`):**
   - Implements dynamic chunking targeting 4-5 words per burst.
   - Enforces a strict character cap (24-32 chars, default 30) to prevent line overflow on 9:16 portrait video.
   - Respects natural punctuation boundaries (hard terminals `. ? !` and soft clauses `, ; :`) and speech pauses (> 0.35s).

2. **Per-Word Isolated Pop Karaoke (`create_caption_block`):**
   - Renders stable multi-word sentence lines where the full phrase remains fixed and visible on screen across all dialogue events.
   - Illuminates ONLY the active word in the highlight color with bold emphasis (`\b1`).
   - Uses vertical-only scaling (`\fscy` transform) during pop animations, keeping horizontal advance widths (`\fscx100`) constant to eliminate sentence dancing / jitter completely.

3. **Single Video Deliverable Cleanup:**
   - Both batch processing (`worker.py`) and on-demand re-rendering (`routes.py`) enforce a single output file contract (`final_video.mp4`).
   - All intermediate scratch files (`short.mp4`, `vertical.mp4`, `final_short.mp4`, `final_with_title.mp4`, `final_with_sfx.mp4`, `with_broll.mp4`, `captions.ass`) are removed post-render.

4. **Automated Verification:**
   - All 9 unit tests in `backend/tests/test_pro_captions.py` and 48 total core backend tests execute cleanly with 100% pass rate.
