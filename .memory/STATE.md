# State — 2026-11-15

## Done earlier (committed as 953c8ed)
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

## Done 2026-11-16 (committed in 953c8ed)
- Free selection model: `OMNIROUTE_MODEL=free/best`, `OMNIROUTE_MODEL_FALLBACKS=free/coding` (config defaults, `.env`, `.env.example`). Both are free-only priority combos on the local OmniRoute. Measured on job 111 with the real selector: free/best 5 `ai` clips in 70 s (served by claude-opus-4-6-thinking), free/coding 5 in 64 s. auto/best-free timed out → grid; pinned agy gemini ids 429 until 11-21.
- `llm_router`: `OMNIROUTE_TIMEOUT_SECONDS=300` (combos fall through members with 120 s each); `_json_object_in` reads text blocks from a thinking content array.
- Tests updated (`test_selection_model_pin.py`, `test_selection_model_chain.py`): forbid `auto/*`, allow `free/*`. 790 passed, 1 skipped.
- Later same day: dedicated combo **`free/shorts`** created in OmniRoute (priority, 13 members, targetTimeout 120 s, comboTimeout 240 s) from a 38-model benchmark on job 111's real prompts. Order: agy/antigravity claude-opus-4-6-thinking + claude-sonnet-4-6 (quota-blocked until ~11-22), agnes-3.0-flash, ernie-5.5-flash, openrouter nemotron-3-ultra, routeway gemma-4, pinned gemini ids, laguna, codestral. Project now `OMNIROUTE_MODEL=free/shorts`, fallback `free/best`. End-to-end on job 111: 5 `ai` clips in 33 s.
- `clip_selector._OMNIROUTE_MIN_TOKENS=12000` (nemotron-3-ultra gave no JSON at 3500).
- OmniRoute pricing: 5 unpriced free models set to $0 via `PATCH /api/pricing` (admin); their usage had blocked every limited key (`fail_closed`), including the project key. Dead today: groq (JSON mode clash / no JSON), sambanova (auth expired), cerebras (credits), mistral medium/magistral (not in catalog).
- Scripts: scratchpad `bench.py`, `rank.py`, `write_combo.py` (not in repo). Tests 791 passed.
- OmniRoute keys live outside the repo (Qoder `draft_secrets.txt`); never copy them into project files.

## Done 2026-11-16 (afternoon, committed as 65e5a87)
- Committed earlier work as 953c8ed. Services restarted 12:57.
- ffmpeg: `utils/env_check.ensure_media_tools_on_path` finds FFmpeg in `C:\Users\*\AppData\Local\Microsoft\WinGet\{Packages\*FFmpeg*\*\bin,Links}` (LocalSystem PATH lacks it) or `FFMPEG_DIR`. Backend self-restarts on idle; confirmed finding ffmpeg 14:07.
- Test run: job 112 (fwW3XVFrm9c, 46 min source, 10 `ai` clips, 33 min) via service; job 113 same URL via own process (`queued` status, scratchpad run_job.py) with new code, 37 min, OK.
- Captions: active-word pop overlapped neighbours ("WASVENTURE") -> per-gap width from pop growth (`_caption_gaps`), end overhang kept inside margins; groups break by pixel width (`_block_fits`, `MIN_GROUP_FIT=0.85`); number/hyphen tokens glued (",000", ".5", "%", "-to").
- Caption names: new `services/caption_fix.py` + `CAPTION_FIX_ENABLED`/`CAPTION_FIX_MODEL=free/best`; LLM returns index->word fixes, similarity/homophone gate, <=25% edits, keeps punctuation; runs as task beside scene attention, 120 s cap; part of clip_words cache key. Whisper tiny..medium all wrote "Traders 8"; free/best fixes it, agnes/gemma/codestral/laguna useless for this. Tests force it off (conftest).
- Focus: `compute_smooth_yolo_trajectory` starts with `cut_since_focus=True` -> before the focus track appears, follow who is on screen (job 112 clip 7 cut the presenter out for 6 s; narrator voice bound to a later track).
- `sample_frames` seeks when interval >= 2 s (temporal sweep decoded all frames: ~11 of 16 min).
- Tests 806 passed, ruff clean.

## Done 2026-11-16 (evening)
- Committed 65e5a87 (afternoon fixes). Services restarted by user 15:23. Deleted jobs 70 and 111 (DB rows + folders; backups `storage/app.db.bak-before-job{70,111}-delete`); unique clip index now created. YouTube still has job 111's QxSMPyB6CBU, LY-o5HuPgPQ.
- Publish: job 113 queued public, 3/day at 06/13/20 UTC (reslotted by hand); `.env` `PUBLISH_SCHEDULE_GAP_SECONDS=25200` (needs worker restart for new jobs).
- Clip ends (uncommitted): new `services/clip_bounds.py` snaps windows to sentence ends from words.json (punctuation first, pauses only as fallback, +0.35 s tail, start on sentence start), extends to finish the thought; `MAX_CLIP_SECONDS` 30 -> 60; prompt aims 25-45 s; heuristic/grid windows snapped too; `cut_clip` adds afade in 0.04 / out 0.25 (part of clip_cut cache key). Job 113 recheck: 10/10 end on a full sentence (5 cut mid-sentence before). Tests 814 passed.

## Known state
- Free OmniRoute models are in 429 cooldown until ~2026-11-21 → job 111 got grid clips and transcript-derived titles. Root cause of "bad titles".
- `ANTHROPIC_API_KEY` not set in `backend/.env` yet. No real Claude call has been made.
- YouTube OAuth token still dead (reconnect in Settings → YouTube).

## Next
- User: add `ANTHROPIC_API_KEY`, restart `ShortsWorker` + `ShortsBackend`, run one job, review clips.
- Not done: full end-to-end render with the new code; single-pass render failures on job 109 not investigated; split-screen for multi-camera edits (speakers never co-visible) is not possible, follow-crop handles those.

## Done 2026-11-18 (committed as 84d0fae; services restarted 09:06)
- Commit ed8b1a7: clips ask for whole topic 35-55 s, MIN_CLIP_SECONDS 30.
- Copied (own code) the Clipify AI ideas, user OK'd non-commercial use: `services/clip_judging.py` (11 dimensions, keep/reject final, penalties, 7 editing modes, topic segmentation + `fit_to_topic`), `Job.clip_mode` (API param `clip_mode`, UI select), `services/gaming_layout.py` (layout `gaming`: gameplay + corner facecam), `services/branding.py` + `api/branding.py` (logo/text, applied after SFX when `branding.json` enabled, `/api/clips/{id}/branding`), `services/narrate.py` + `api/narrate.py` (faceless shorts: script, SAPI/Kokoro/ElevenLabs voice, library footage, captions), `services/live_studio.py` + `api/live.py` (live buffer, highlights, instant clip). Credit in `backend/NOTICE-third-party-ideas.md`.
- Behaviour change: judge reject is final; all rejected -> no clips; fewer good clips than asked is not padded with heuristic filler.
- Tests 850 passed. NOT tried on a real video, not in the UI yet for branding/narrate/live (API only).
- Cleanup (uncommitted): removed dead scripts, `_composite_score`, `_RANKING_SCHEMA`, `select_best_clips`; `llm_router.pick_model()` replaces 3 copies; old OpenCV vertical (`make_vertical*`, `compute_crop_box`, `sample_frames_for_face_tracking`) removed, `smart_crop_video` is now one ffmpeg encode from `build_vertical_filter`. Evidence: single-pass failed 0 times since 2026-11-12 (84 successes). Tests 848. Job 115 (new judging, 35-55 s) was running, not reviewed.
- 2026-11-18 (uncommitted): Narrate + Live now tabs in the main page (`static/js/studios.js`, tabs Clips/Narrate/Live). Narrate finds footage itself from Pexels (`services/stock_footage.py`, key in .env; NOT YouTube/gameplay: copyright), captions use the script words timed by Whisper (`align_script_to_words`). Live: yt-dlp via `python -m yt_dlp`, video+audio HLS = 2 inputs, transcription decodes with ffmpeg (faster-whisper/PyAV `metadata_errors` bug). Real tests done: Narrate EUV/Wow-signal video with Zira voice (test_stock), Live on Al Jazeera English (3 auto clips). User wants English only. Tests 864.
- Open: service needs restart (admin PowerShell) to load narrate/live fixes; live clips crop news graphics at the sides (face-follow), fit_blur would keep them.
- 2026-11-18 evening: Live Studio takes `context` (stream title + user text), LLM names clips (title+description), keyword fallback only when the model fails; gaming layout detects small edge facecams. Uploaded to YouTube public (not queued): KjkRGUKDRPU, wL6JlAdVc0w (old weak titles/blur layout), OcpeoZIfPXA, yiGOKQegCWI (IShowSpeed GTA 6 live, qSyo5d-dVzA). GTA 6 official release 2026-11-19 (preload 11-12); stream says early access, unverified. Uncommitted.

## Done 2026-11-21 (committed as 4f4dfb4 and 7fea202, not pushed)
- 4f4dfb4: Narrate + Live tabs, Pexels stock footage, live context, edge facecam detection (the 2026-11-18 "uncommitted" items are now committed).
- 7fea202: live clips use a fixed webcam panel on top (672 px) and cover-cropped gameplay below (1248 px) in 1080x1920. Webcam auto-detected per session: face boxes pooled on a 16:9 grid (16000x9000), locked when one corner has >=8 hits and >=80% of corner hits (`LiveSession._find_facecam`, `gaming_layout.pick_facecam`, `sample_faces`). Unlocked clips use their own box or `smart_crop_video`.
- Live capture: yt-dlp format `bv*[height<=1080]+ba/b[height<=1080]/b` (was 720). Twitch channels accepted (`TWITCH_RE`). Concat encode adds `aresample=async=1:first_pts=0` (audio-delay fix, not confirmed by the user). Title overlay via `motion_title.add_motion_title` on live clips.
- Verified on Speed and Kai (cam found, locked, layout correct). yt3 (TimTheTatman) never locks: static top-right graphic (~17 hits) beats the real top-left cam (10 hits); falls back to normal framing.
- Tests: 868 passed, 1 skipped.

## Pending (2026-11-21)
- Services need restart (`Restart-Service ShortsBackend, ShortsWorker`, admin) before UI/worker use the 1080p and layout code. No live run since the change; no 1080p clip seen yet.
- Live capture has no reconnect: capture died after ~32 min in the last multi-stream run. Needs auto-reconnect until the stream really ends.
- Twitch ad breaks ("Commercial break in progress") get captured into clips. No filter.
- yt3 webcam detection (static false face). Possible fix: ignore corners with static graphics or require the box to move with the face.
- Audio delay: not reproduced by measurement; aresample fix unverified.
- Weak titles already uploaded (e.g. "... moment" / 3-word titles). Edit only in YouTube Studio (OAuth scope lacks edit/delete).
- Multi-stream relaunch (`storage/live_multi`, GAP=180 s uploads): not started; user chose commit only.

## Done 2026-11-21 (evening, uncommitted)
- Webcam finder rewritten: `services/facecam_finder.py`. The Haar face detector never found the streamer's face in the webcam (it hit a static doll on a shelf, which then got locked). Now YOLO person detection (`focus_tracking._get_yolo`, the project's yolov8n) in corners and edges; a person narrower than 22% of the frame, seen on >=50% of 24 samples, counts. Head box centres the panel. Verified: TimTheTatman (top-left) and IShowSpeed (left edge) render correctly; Kai bottom-left.
- Lock in `LiveSession._find_facecam`: locked when the same spot appears in 2 clips or hit rate >=80%. `panel_for_face` extracted in `gaming_layout`; `CAM_FACE_SPAN` 4 -> 5.
- Titles: `LiveSession(topic=...)` -> `llm_highlight`/`heuristic_highlight` keep the topic in every title (`_keep_topic`). Multi-live script uses topic "GTA 6" and no hardcoded cam.
- Tests: 874 passed, 1 skipped (new `tests/test_facecam_finder_and_titles.py`). One real LLM spot-check: "GTA 6 Stolen Car Chase Ends in Water".
- Limitation: Tim's webcam is ~320 px wide in the source, so the zoomed panel is soft at 1080.

## Done 2026-11-22 (branch `editing-intelligence`, not pushed; details in `docs/EDITING_INTELLIGENCE.md`)
- Also earlier on the branch: LLM health indicator + readable job errors (fd9ddac), titles <= 40 chars with complete-phrase fallback, job 117 requeued.
- Phase 0 360fe97: Greek emoji match + somber gate, `tone_category` stored. Phase 1 0baf165: colour emoji stickers, title centred on webcam seam. Phase 2 92e27d4: `services/editing/` timeline + profiles + planner. Phase 3 c06283e: zoom/shake/flash/tint. Phase 4 916f321: sound design + sidechain on SFX bus. Phase 5 a40405a: caption motions, popup clears webcam. Phase 6 3bbd981: technical quality evaluator, render manifest + fingerprint, publish gate on `failed`. Phase 7 1fb03ee: title claim check, advisory editorial evaluator (off by default, paid).
- New DB columns on Clip: `tone_category`, `edit_profile`, `quality_report`, `render_manifest`, `render_fingerprint`. New endpoints: `GET /clips/{id}/quality`, (uncommitted) `GET /clips/{id}/edit`, `POST /clips/{id}/ab-check`.
- Tests at Phase 7 commit: 1178 passed, 1 skipped.
- Phase 9 uncommitted: visual regression (`services/visual_regression.py`, golden scene in `tests/visual/`), A/B effect check (`services/editing/ab.py`), plain preview render (`RENDER_PLAIN_PREVIEW`, default off), `api/editing.py`, `static/js/timeline_view.js` (unreviewed, unwired). Real 12 s render: all four effects detected as visible by the A/B check.

## Pending 2026-11-22
- Finish Phase 9: review + wire `timeline_view.js`, tests `test_ab_check.py`/`test_editing_api.py`, browser check, full suite, commit.
- Restart services, run one real job, review clips; decide on `RENDER_PLAIN_PREVIEW` / `EDITORIAL_EVALUATOR_ENABLED`.
- Re-render parity (`_rerender_media`), caption style precedence, anti-fingerprint study, true peak -0.04 dBTP, `reproduce_render.py`, editorial calibration, Phase 8 music, project skills, SFX provenance for the 31 old files, push + PR.

## Done 2026-11-22 (Phase 9 finished; uncommitted, waiting for approval)
- Work lives in the recovery worktree `claude/editing-intelligence-recovery-925a80`: fast-forwarded to `1fb03ee`, Phase 9 applied on top. The main checkout still holds its older uncommitted Phase 9 copy; do not commit from it without reconciling.
- `static/js/timeline_view.js` reviewed (textContent only, no change needed), wired: `#studioEditDetails` / `#studioEditPanel` in `templates/index.html`, lazy load in `studio.js`, `.edit-*` CSS in `styles.css`.
- Tests: `tests/test_ab_check.py` (16) and `tests/test_editing_api.py` (16). Full backend suite: **1226 passed, 3 skipped, 0 failed** (baseline 1178 + 1 skipped, plus 50 new = 1229).
- Two suite failures fixed: `test_burned_in_title` now reads the `_rsp_args` block; `/media` guard checks only the path inside `STORAGE_ROOT` (the worktree lives under `.claude\`, which blocked every media file).
- Browser check on an isolated scratch DB/storage (backend on port 5055, two synthetic clips, live watcher off): panel renders; "Check effects" runs real ffmpeg (control SSIM 0.999 / PSNR 55; red-box effect visible); timeline-only clip shows "Quality not checked" and no compare block; Style/Cut/AI tabs and Publish unaffected; no console errors.
- Not done: production validation (services not restarted, no real job with `RENDER_PLAIN_PREVIEW`), playback-sync check of the Before/After transport, Phase 8, and the rest of the list above.
