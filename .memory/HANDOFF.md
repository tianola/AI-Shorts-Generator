# Handoff — 2026-11-22 (Phase 9 finished, uncommitted)

- Work location: recovery worktree `E:\01-AI-PROJECTS\ai-shorts-generator\.claude\worktrees\editing-intelligence-recovery-925a80` (branch `claude/editing-intelligence-recovery-925a80`, at `1fb03ee` plus uncommitted Phase 9). The main checkout `E:\01-AI-PROJECTS\ai-shorts-generator` (branch `editing-intelligence`) still holds its older uncommitted copy of Phase 9. Do not commit from there without reconciling.
- Read `docs/EDITING_INTELLIGENCE.md` first, then `.memory/STATE.md` (bottom section).
- Commit Phase 9 only after the user approves. Full backend suite at this state: 1226 passed, 3 skipped, exit 0.
- Interpreter: the worktree has no `.venv` (gitignored). Use `E:\01-AI-PROJECTS\ai-shorts-generator\backend\.venv\Scripts\python.exe` with `PYTHONIOENCODING=utf-8`, run from the worktree's `backend` folder.
- Not done: production validation (restart `ShortsBackend` and `ShortsWorker`, then one real job with `RENDER_PLAIN_PREVIEW` on), playback-sync check of the Before/After transport, Phase 8, re-render parity (`_rerender_media`), -0.04 dBTP, `reproduce_render.py`, editorial calibration.
- Rules: tests never call a real LLM; every ffmpeg call has `timeout=`; voice stays at unity; titles must be true to the clip; editorial findings stay advisory; no changes tuned for Content ID evasion.
