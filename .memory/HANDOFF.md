# Handoff — 2026-11-21

- Read `.memory/STATE.md` first. Latest commits on `master`: 7fea202 (live webcam layout, 1080p) and 4f4dfb4 (Narrate/Live). Nothing pushed.
- NSSM services still run the code from before 7fea202. Restart needs admin PowerShell: `Restart-Service ShortsBackend, ShortsWorker`.
- Live clipping: single-stream `auto_live.py` and multi-stream `multi_live.py` live in scratchpad (not in repo). Use `backend/.venv/Scripts/python.exe`, never the system Python.
- Open: live capture auto-reconnect, Twitch ad-break filter, yt3 webcam lock, audio-delay verification, relaunch of the lives.
- Tests must never call the real Anthropic API. Run with `backend/.venv/Scripts/python.exe -m pytest -q` (868 passed, 1 skipped on 7fea202).
- Obsidian notes: `E:\Documents\Obsidian Vault\AI-SECOND-BRAIN\PROJECTS\ai-shorts-generator\` (STATUS note has a 2026-11-21 entry).
