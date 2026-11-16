# Project

- Local-first short video generator: FastAPI/SQLAlchemy backend, SQLite, worker pipeline, FFmpeg, Whisper, YOLO, YouTube/TikTok publishing.
- `backend/api/`: API routes; `backend/services/`: selection, transcription, editing, rendering and platform clients; `backend/worker.py`: job orchestration.
- Clip selection reads `segments.json`, optional temporal events and audio energy; renders selected time windows downstream.
- LLM routing: OmniRoute/OpenAI-compatible + Ollama fallback in `backend/services/llm_router.py`; Anthropic client in `backend/services/claude_client.py` (clip selection only).
- Settings live in `backend/config.py`; backend dependencies in `backend/requirements.txt`; tests in `backend/tests/`.
- Never copy secrets from `.env` into docs. Test DB/log paths must be isolated (`backend/tests/conftest.py`).
