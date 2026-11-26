# Project

- Local-first short video generator: FastAPI/SQLAlchemy backend, SQLite, worker pipeline, FFmpeg, Whisper, YOLO, YouTube/TikTok publishing.
- `backend/api/`: API routes; `backend/services/`: selection, transcription, editing, rendering and platform clients; `backend/worker.py`: job orchestration.
- Clip selection reads `segments.json`, optional temporal events and audio energy; renders selected time windows downstream.
- LLM routing: OmniRoute/OpenAI-compatible + Ollama fallback in `backend/services/llm_router.py`; Anthropic client in `backend/services/claude_client.py` (clip selection only).
- Settings live in `backend/config.py`; backend dependencies in `backend/requirements.txt`; tests in `backend/tests/`.
- Never copy secrets from `.env` into docs. Test DB/log paths must be isolated (`backend/tests/conftest.py`).
- Editing intelligence (branch `editing-intelligence`, see `docs/EDITING_INTELLIGENCE.md`): `backend/services/editing/` (timeline, profiles, planner, effect_filters, sound, ab), `backend/services/quality/` (technical, editorial, pipeline), `services/render_manifest.py`, `services/title_claims.py`, `services/visual_regression.py`, `api/editing.py`. Each clip writes `edit_timeline.json`, `quality.json`, `render_manifest.json` in its folder.
- Rules: every ffmpeg call has `timeout=`; voice branch at unity (only SFX bus ducked); no random filter expressions; titles true to the clip; editorial findings advisory; tests never call a real LLM.
