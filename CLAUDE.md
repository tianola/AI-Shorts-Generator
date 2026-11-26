# CLAUDE.md

Local-first pipeline that turns long videos into vertical shorts and publishes
them to YouTube and TikTok. Python 3.11, FastAPI, SQLAlchemy on SQLite, FFmpeg,
Whisper, YOLO. Runs on the owner's Windows machine as two processes: the API
(`backend/main.py`, NSSM service) and the worker (`backend/worker.py`).

Longer notes for agents live in `.memory/` (`PROJECT.md`, `STATE.md`,
`HANDOFF.md`). Read `STATE.md` before changing the pipeline.

## Layout

- `backend/worker.py`: job orchestration. It is the hottest file in the repo and
  usually changes together with `services/clip_selector.py`, `config.py` and
  `database/models.py`. Check all four when you touch one.
- `backend/api/`: FastAPI routes. `backend/services/`: selection, transcription,
  reframing, captions, render, platform uploads.
- `backend/config.py`: every setting. A new setting also goes in
  `backend/.env.example` with a comment.
- `backend/static/js/`, `backend/templates/index.html`: the dashboard (plain JS).
- `scripts/diagnostics/`: one-off local checks, not tests.

## Commands

```bash
python -m pytest -q --timeout=180          # from the repo root; pytest.ini sets paths
ruff check backend --select F,E9           # CI fails on any hit
for f in backend/static/js/*.js; do node --check "$f"; done
```

Tests never touch the real database: `backend/tests/conftest.py` forces
`TEST_DATABASE_URL=sqlite:///:memory:` and blanks API keys. Keep it that way.
In the cloud sandbox torch, pyannote, ultralytics and mediapipe can't be
installed, so the torch-based diarization tests fail there and nowhere else.

## Rules

- The API binds to 127.0.0.1 by default. Don't widen that without a token.
- The default LLM path is the free OmniRoute chain. Claude passes stay opt-in
  behind `ANTHROPIC_API_KEY`.
- Every publish goes through the publish queue, which records intent before
  the upload. Don't call the YouTube or TikTok uploaders directly.
- Never copy keys from `.env` into code, docs or `.memory/`.
- Commit messages: imperative sentence saying what changed for the user, e.g.
  "Stop a disconnected channel from retiring clips". No `feat:`/`fix:` prefix.
  One logical change per commit, with its tests.
