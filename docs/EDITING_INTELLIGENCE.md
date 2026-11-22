# Editing intelligence (branch `editing-intelligence`)

Status at 2026-11-22. Phases 0-7 committed on the branch (not pushed, not merged to `master`). Phase 9 finished in the worktree (panel wired, tests, browser check); uncommitted, waiting for approval. Phase 8 not started. Full plan: `C:\Users\thana\.claude\plans\declarative-wiggling-lemur.md` (outside the repo).

## What it does
- Every clip gets an **edit timeline**: which moments matter (hook, emotional peak, stressed sentence, question, loud peak, scene cut, popup), which effects and sounds they earn, and why. Effects, sound, captions, quality check and studio view all read the same file.
- Rendering stays a single ffmpeg pass (`services/render.py`); the timeline only adds filter fragments. Sequential fallback skips the effects and says so in the job log.
- After the render, the shipped file is measured and the way it was made is recorded (manifest + fingerprint).

## Phases and commits
| Phase | Commit | Content |
|---|---|---|
| 0 | 360fe97 | Greek-aware emoji match (`captions._fold`, Greek stems), Greek somber tone gate, `tone_category` saved on the clip |
| 1 | 0baf165 | Colour emoji stickers (Pillow `embedded_color`, Segoe UI Emoji, sidecar `*.emoji.json`, overlay stage); title card cropped to its alpha bbox and centred on the webcam seam (`render._title_center_frac`) |
| 2 | 92e27d4 | `services/editing/` timeline, profiles, deterministic planner (budgets, spacing, tone gate, recorded refusals), bridge, describe |
| 3 | c06283e | Visual effects: zoom punch, shake, flash, red tint (`editing/effect_filters.py`, overlay-based, no black borders) |
| 4 | 916f321 | Sound design from the timeline (`editing/sound.py`), sidechain on the SFX bus only (voice stays at unity), `assets/sfx/PROVENANCE.md` |
| 5 | a40405a | Caption motion profiles (`CAPTION_MOTIONS`), emphasis words, popup `game_top` position clears the webcam |
| 6 | 3bbd981 | Technical quality evaluator (`services/quality/`), render manifest + SHA-256 fingerprint, publish gate on `failed`, anti-fingerprint `PARAMS` + timeout |
| 7 | 1fb03ee | Title claim check (`services/title_claims.py`), advisory editorial evaluator (`services/quality/editorial.py`), Claude vision call |
| 8 | - | Optional music bed. Not started (needs licensed assets) |
| 9 | uncommitted | Plain preview render, A/B effect check, visual regression, studio panel (`static/js/timeline_view.js`, loaded when "How this clip was edited" is first opened in the clip editor) |

Earlier on the same line of work (before Phase 0): LLM health indicator (`services/llm_health.py`, `static/js/llm_status.js`), readable job errors, "Job creation failed: [object Object]" fix, titles <= 40 chars with complete-phrase fallback, job 117 requeue.

## Data contracts (all versioned JSON)
- `Event {id, kind, start, end, intensity, confidence, priority, source, tone, visual_actions[], audio_actions[], reason}`; times are clip-relative seconds, before any speed change.
- `Timeline {version, clip_duration, profile, tone, events[], visual_tracks[], audio_tracks[], suppressed[{event_id, action, reason}], metadata}` -> `edit_timeline.json`.
- `Finding {id, category, severity, source, issue, explanation, confidence, evidence{kind, ref, value}, recommendation, timestamp_clip, timestamp_final, evaluator_version, stable, runs_seen}`. A finding with no evidence is dropped. No quality score anywhere.
- `QualityReport {version, status: passed|review|failed, findings[], checks{}, evaluator_versions, agreement_rate, corrections, metadata}`; stored with optional `editorial` and `title_check` blocks.
- Render manifest: renderer version, git commit, ffmpeg version, profile, settings, asset SHA-256 (emoji, SFX, fonts), anti-fingerprint params, final filtergraph; fingerprint = SHA-256 of it.

## Files written per clip (in `storage/jobs/<job>/clip_<n>/`)
`edit_timeline.json`, `quality.json`, `render_manifest.json`, `editorial.json` + `editorial_frames/` (only when enabled), `final_plain.mp4` (only when enabled), `ab_report.json` (after an A/B check). `*.emoji.json` sidecars are pruned after render.

## DB columns (Clip, auto-added by `_sync_columns`, nullable)
`tone_category`, `edit_profile`, `quality_report` (JSON), `render_manifest` (JSON), `render_fingerprint`. All in `worker.CLIP_RECORD_FIELDS` and `api/common.clip_payload` (`quality` is a status + counts summary).

## API
- `GET /api/clips/{id}/quality` -> report, manifest, fingerprint.
- `GET /api/clips/{id}/edit` -> timeline, explanations, quality, urls of final and plain file, speed, A/B result. (Phase 9)
- `POST /api/clips/{id}/ab-check` -> compares plain vs finished at every planned effect. 409 when there is no timeline, no plain file or no effects. (Phase 9)

## Config flags (`backend/config.py`)
- Defaults on: `EFFECTS_ENABLED`, `EFFECTS_DISABLED_KINDS`, `SOUND_DESIGN_ENABLED`, `SFX_SIDECHAIN_ENABLED`, `SFX_DUCK_THRESHOLD/RATIO/ATTACK_MS/RELEASE_MS`, `CAPTION_EMOJI_MODE`, `EMOJI_FONT_PATH`, `TITLE_CLAIM_CHECK_ENABLED` (free chain, fails open).
- Defaults off: `EDITORIAL_EVALUATOR_ENABLED` (paid Claude call, 3 runs per clip, needs `ANTHROPIC_API_KEY`; `EDITORIAL_RUNS`, `EDITORIAL_FRAME_COUNT`, `CLAUDE_EDITORIAL_MODEL=claude-sonnet-5-5`), `RENDER_PLAIN_PREVIEW` (one extra single-pass render per clip).

## Rules the code keeps (do not break)
- Voice branch at unity; only the SFX bus is ducked (`tests/test_sfx_voice_preservation.py`).
- Every ffmpeg/ffprobe call has `timeout=` (`tests/test_render_timeouts.py` lists the modules, including `quality/technical.py`, `quality/editorial.py`, `anti_copyright.py`).
- No random expressions in filters (reproducible). At most one flash per second; tone gate removes tint/flash on somber content.
- Titles must be true to the clip. A rejected title becomes the clip's own words; nothing invents claims.
- Tests never call a real LLM; `tests/conftest.py` blanks `ANTHROPIC_API_KEY`.
- Editorial findings are advisory: they never change `status` and never block a clip.

## Verification so far
- Full suite at Phase 7 commit: 1178 passed, 1 skipped. Phase 9 adds `tests/test_visual_regression.py` and `tests/visual/` (18 passed in isolation); full suite not rerun since.
- Real data: job 117 clips 3 and 5 `passed`; missing file -> `failed`; 11 s effects render -> `review` (true peak -0.04 dBTP).
- A/B check on a 12 s real render: zoom, shake, flash, tint all `visible`; plain vs plain none visible. Control frame SSIM 0.9965 / PSNR 49 dB; tint frame PSNR 33 dB (SSIM alone barely moves, so PSNR is part of the rule).
- Golden scene `tests/visual/baselines/scene/` (4 frames: title, caption, zoom, tint) approved by Claude after viewing, not yet by the user. Re-approve with `python scripts/visual_baseline.py capture` after looking at the frames.
- 2026-11-22 Phase 9 finish (worktree `claude/editing-intelligence-recovery-925a80`, fast-forwarded to 1fb03ee, Phase 9 applied on top, uncommitted):
  - `tests/test_ab_check.py` (16 tests: control moment, visible rules for SSIM and PSNR, speed mapping 1/speed, 12-item cap, failed frames, report file) and `tests/test_editing_api.py` (16 tests, parameters counted: GET/POST guards 404/409, corrupt timeline, speed from manifest, 500 leaves no report). Frame comparison faked there; real ffmpeg covered by `test_visual_regression.py`.
  - `timeline_view.js` reviewed: all model text via `textContent`, errors shown in the panel, no change needed. Wired: `<details id="studioEditDetails">` + `#studioEditPanel` in `templates/index.html`, `studio.js` loads it on first open per clip, resets on open/close. CSS `.edit-*` added to `static/styles.css`. `node --check` passes for every `static/js` module (CI method).
  - Browser check on an isolated scratch DB and storage (two synthetic clips, backend on port 5055): panel renders timeline, reasons, quality and saved A/B table; "Check effects" runs real ffmpeg (control SSIM 0.999 / PSNR 55 dB, red-box effect visible, SSIM 0.76 / PSNR 11.6); timeline-only clip shows "Quality not checked" and no before/after block; Style/Cut/AI tabs and Publish unaffected; no console errors.
  - Real-data fixture lesson: a fake "finished" file must carry the same 1.02 speed change as the manifest, or the control frame drifts (control SSIM fell to 0.86 before the fix).
  - Two full-suite failures found and fixed: (1) `test_burned_in_title` read the `_rsp(` call text, which Phase 9 moved into `_rsp_args`; the test now reads that block. (2) `/media` guard blocked every file under a dot-folder above the storage root (the worktree lives under `.claude\`); the guard now checks only the path inside `STORAGE_ROOT`.

## Known issues and findings
- True peak -0.04 dBTP on the test render although loudnorm targets -1.5 (AAC/loudnorm overshoot, or the anti-fingerprint chain). Not fixed.
- Anti-fingerprint is a second full encode (+2% speed, hue 6, saturation 1.05, pitch +1.5 semitones). Timeline times are pre-speed; findings store both `timestamp_clip` and `timestamp_final`. Fold-in study not done. No change will be tuned to defeat Content ID; the transform is only made correct, bounded and measurable.
- `services/anti_copyright.py` states a Content-ID purpose; platform/copyright risk of other people's content is the user's to weigh.
- Re-render (`api/clips.py::_rerender_media`) still uses the old sequential pipeline: no effects, no sound design, no timeline, no quality check, no popup/layout parity. Re-rendering a clip therefore loses the new edits. Deferred.
- Job-vs-clip caption style precedence unchanged (job default still overrides per-clip style).
- The 31 older SFX files are unverified placeholders; only the new ones have provenance.
- Number check in `title_claims` produced false positives on real titles ("3 Words", "$2.5M", "$360 million"), so it is only a hint to the model; the verdict comes from the model with a quote that must appear in the transcript. Never run against a real model yet: replacement rate unknown.
- Editorial evaluator has never run against the real API; precision unmeasured. Needs 5 clips labelled by the user before it may drive any correction.
- Placeholder titles "VIRAL MOMENT N" exist in old jobs (pre-title-fix).

## To do (ordered)
1. **Commit Phase 9** (approval needed). Done 2026-11-22: (a) review of `timeline_view.js`, (b) wiring, (c) tests, (d) browser check, (e) full suite. Original task list: (a) review `static/js/timeline_view.js` (519 lines, written by a Haiku agent, unreviewed; must use `textContent` only), (b) wire it: `<details id="studioEditDetails"><div id="studioEditPanel">` in `templates/index.html` and a first-toggle call to `renderEditPanel(clip.id, panel)` in `static/js/studio.js::openClipStudio`, plus CSS, (c) write `tests/test_ab_check.py` and `tests/test_editing_api.py` (agents hit the usage limit; resets 16:40 Athens), (d) check the panel in the browser, (e) full suite, commit.
2. Restart services (`Restart-Service ShortsBackend, ShortsWorker`, admin PowerShell; restarting the worker interrupts a running job) and run one real job; look at the log lines `Edit plan`, `Sound plan`, `Quality`, `Title replaced`.
3. User: look at the baseline frames and the first real clips; decide whether `RENDER_PLAIN_PREVIEW` and `EDITORIAL_EVALUATOR_ENABLED` go on.
4. Re-render parity: make `_rerender_media` reuse the worker's single-pass builder (timeline, effects, sound, popups, layout, quality). Fix style precedence at the same time.
5. Anti-fingerprint study (`scripts/compare_antifp.py`, plan 7.4 table); fold into the single pass only if every row passes; otherwise keep the second pass, bound it, and measure final loudness.
6. Fix or explain the -0.04 dBTP true peak.
7. `scripts/reproduce_render.py` (re-render from a manifest; SSIM >= 0.999, audio difference under a threshold).
8. Editorial calibration on 5 user-labelled clips; only then allow corrections.
9. Phase 8 music (optional, licensed assets only).
10. Project skills `shorts-render-verify` and `shorts-editing-rules` in `.claude/skills/` + CLAUDE.md pointer.
11. Verify or replace the 31 older SFX placeholders.
12. Push the branch and open a PR (PR #2 belongs to the older branch).

## How to run and test
- Interpreter: `backend/.venv/Scripts/python.exe` (never the system Python). Set `PYTHONIOENCODING=utf-8`.
- Suite: `cd backend; ./.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider` (about 2-3.5 min).
- Real DB is `storage/app.db` (not `backend/storage`). Backend port 5001.
- Helper scripts: `scripts/benchmark_render.py`, `compare_media.py`, `build_sfx.py`, `sfx_provenance.py`, `visual_baseline.py`.
- Delegation rule from the user: Haiku subagents for easy work (tests, scripts, small edits); the main model does design and verification. Verify every agent claim by running the tests.
