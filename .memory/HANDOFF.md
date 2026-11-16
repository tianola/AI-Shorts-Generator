# Handoff — 2026-11-15

- Read `.memory/STATE.md` first; all changes there are uncommitted on `master` and the NSSM services still run the old code.
- To activate: set `ANTHROPIC_API_KEY` in `backend/.env` (do not print env values), restart `ShortsWorker` and `ShortsBackend` (needs UAC), run one job.
- Verify on that job: log line `Clip selection path=claude: N of M candidate windows (ranked by second pass)`, headline/captions look, crop after camera cuts.
- If Claude fails or no key: selector falls back to OmniRoute (`selection_path=ai`); if that is down too, clips are `grid` and are not auto-published.
- Tests must never call the real Anthropic API (`tests/conftest.py` blanks the key). Run with `backend/.venv/Scripts/python.exe -m pytest -q`.
- Open: commit (user has not asked), end-to-end render check, job 109 single-pass render failures.
- Obsidian notes: `E:\Documents\Obsidian Vault\AI-SECOND-BRAIN\PROJECTS\ai-shorts-generator\` (STATUS note updated 2026-11-15).
