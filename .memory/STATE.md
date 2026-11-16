# State — 2026-11-15

## Done this session (uncommitted, services NOT restarted)
- Clip selector rewritten (`backend/services/clip_selector.py`): two passes (≈2x candidates → ranking pass on the words inside each window), windows placed on segment boundaries by `_fit_window`, overlap dedupe, `selection_path` on every clip (`claude` / `ai` / `heuristic` / `grid`), English keyword scores no longer rank (`_selection_score`).
- Claude for selection only: `services/claude_client.py`, settings `ANTHROPIC_API_KEY`, `CLAUDE_SELECTION_MODEL=claude-opus-5-5`. No key → OmniRoute chain. `anthropic` installed in `backend/.venv`.
- Titles: selector prompt asks for a teasing headline in the spoken language; `generate_metadata(keep_title=True)` keeps it for model picks.
- Publish: `enqueue_clips_for_publish` skips `heuristic`/`grid` clips unless `include_unreviewed=True` (manual upload); TikTok sweep skipped when a job holds such clips. Provenance stored in `Clip.score_breakdown`.
- Follow-crop (`services/focus_tracking.py`): detects shot changes, jumps instead of panning, follows the visible person after a cut.
- Headline card (`services/motion_title.py`): balanced lines, heavy face with Greek glyphs, highlighted key word, Greek capitals without tonos.
- Captions (`services/captions.py`, `services/render.py`): bundled font passed to libass via `fontsdir`, preset sizes are real em px (`_ass_font_size`), uppercase for mrbeast/hormozi, fit-to-width, Greek → Segoe UI Black, `CAPTION_GROUP_SIZE` default 3.
- `MIN_SPEAKER_SHARE` 0.30 → 0.20 (split-screen for two co-visible speakers).
- Bug fixed: `_blocked_script` crashed (`text` → `txt`) instead of holding a foreign-script title.
- Tests: 789 passed, 1 skipped; ruff F,E9 clean. New: `tests/test_two_pass_selection.py`, `tests/test_shot_change_follow.py`. `tests/conftest.py` blanks `ANTHROPIC_API_KEY`.

## Done 2026-11-16 (uncommitted)
- Free selection model: `OMNIROUTE_MODEL=free/best`, `OMNIROUTE_MODEL_FALLBACKS=free/coding` (config defaults, `.env`, `.env.example`). Both are free-only priority combos on the local OmniRoute. Measured on job 111 with the real selector: free/best 5 `ai` clips in 70 s (served by claude-opus-4-6-thinking), free/coding 5 in 64 s. auto/best-free timed out → grid; pinned agy gemini ids 429 until 11-21.
- `llm_router`: `OMNIROUTE_TIMEOUT_SECONDS=300` (combos fall through members with 120 s each); `_json_object_in` reads text blocks from a thinking content array.
- Tests updated (`test_selection_model_pin.py`, `test_selection_model_chain.py`): forbid `auto/*`, allow `free/*`. 790 passed, 1 skipped.
- Later same day: dedicated combo **`free/shorts`** created in OmniRoute (priority, 13 members, targetTimeout 120 s, comboTimeout 240 s) from a 38-model benchmark on job 111's real prompts. Order: agy/antigravity claude-opus-4-6-thinking + claude-sonnet-4-6 (quota-blocked until ~11-22), agnes-3.0-flash, ernie-5.5-flash, openrouter nemotron-3-ultra, routeway gemma-4, pinned gemini ids, laguna, codestral. Project now `OMNIROUTE_MODEL=free/shorts`, fallback `free/best`. End-to-end on job 111: 5 `ai` clips in 33 s.
- `clip_selector._OMNIROUTE_MIN_TOKENS=12000` (nemotron-3-ultra gave no JSON at 3500).
- OmniRoute pricing: 5 unpriced free models set to $0 via `PATCH /api/pricing` (admin); their usage had blocked every limited key (`fail_closed`), including the project key. Dead today: groq (JSON mode clash / no JSON), sambanova (auth expired), cerebras (credits), mistral medium/magistral (not in catalog).
- Scripts: scratchpad `bench.py`, `rank.py`, `write_combo.py` (not in repo). Tests 791 passed.
- OmniRoute keys live outside the repo (Qoder `draft_secrets.txt`); never copy them into project files.

## Done 2026-11-16 (afternoon, uncommitted; worker service NOT restarted - UAC denied twice)
- Committed earlier work as 953c8ed. Services restarted 12:57.
- ffmpeg: `utils/env_check.ensure_media_tools_on_path` finds FFmpeg in `C:\Users\*\AppData\Local\Microsoft\WinGet\{Packages\*FFmpeg*\*\bin,Links}` (LocalSystem PATH lacks it) or `FFMPEG_DIR`. Backend self-restarts on idle; confirmed finding ffmpeg 14:07.
- Test run: job 112 (fwW3XVFrm9c, 46 min source, 10 `ai` clips, 33 min) via service; job 113 same URL via own process (`queued` status, scratchpad run_job.py) with new code, 37 min, OK.
- Captions: active-word pop overlapped neighbours ("WASVENTURE") -> per-gap width from pop growth (`_caption_gaps`), end overhang kept inside margins; groups break by pixel width (`_block_fits`, `MIN_GROUP_FIT=0.85`); number/hyphen tokens glued (",000", ".5", "%", "-to").
- Caption names: new `services/caption_fix.py` + `CAPTION_FIX_ENABLED`/`CAPTION_FIX_MODEL=free/best`; LLM returns index->word fixes, similarity/homophone gate, <=25% edits, keeps punctuation; runs as task beside scene attention, 120 s cap; part of clip_words cache key. Whisper tiny..medium all wrote "Traders 8"; free/best fixes it, agnes/gemma/codestral/laguna useless for this. Tests force it off (conftest).
- Focus: `compute_smooth_yolo_trajectory` starts with `cut_since_focus=True` -> before the focus track appears, follow who is on screen (job 112 clip 7 cut the presenter out for 6 s; narrator voice bound to a later track).
- `sample_frames` seeks when interval >= 2 s (temporal sweep decoded all frames: ~11 of 16 min).
- Tests 806 passed, ruff clean.

## Known state
- Free OmniRoute models are in 429 cooldown until ~2026-11-21 → job 111 got grid clips and transcript-derived titles. Root cause of "bad titles".
- `ANTHROPIC_API_KEY` not set in `backend/.env` yet. No real Claude call has been made.
- YouTube OAuth token still dead (reconnect in Settings → YouTube).

## Next
- User: add `ANTHROPIC_API_KEY`, restart `ShortsWorker` + `ShortsBackend`, run one job, review clips.
- Not done: full end-to-end render with the new code; single-pass render failures on job 109 not investigated; split-screen for multi-camera edits (speakers never co-visible) is not possible, follow-crop handles those.
