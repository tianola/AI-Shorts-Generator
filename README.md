# AI Shorts Generator

A local-first AI short-video pipeline for generating vertical shorts from long-form videos. It supports:

- audio extraction and whisper transcription
- clip selection and trimming
- vertical conversion and subtitle burn-in
- metadata generation
- optional ElevenLabs TTS narration
- thumbnail generation
- job persistence to SQLite

## Architecture

- `backend/` — FastAPI app, worker, service layer, config, DB models
- `storage/jobs/<job_id>/` — generated media, metadata, logs
- `n8n_workflows/` — automation flows
- `bridge/` — optional integration layer

## Requirements

Install these before running the pipeline locally:

- Python 3.11+
- FFmpeg on PATH
- OmniRoute OpenAI-compatible endpoint at `http://localhost:20128/v1` for all LLM prompts
- Optional fallback: Ollama running locally on `http://host.docker.internal:11434`
- Optional: ElevenLabs API key for TTS

## One-click local start

On Windows, from the repository root, run:

```powershell
./start_ai_shorts.ps1
```

This will:

- create the backend virtual environment if it is missing
- install dependencies
- start the API on `http://localhost:8001/docs`
- start the worker in the background
- write logs to `storage/logs/`

## Quick start

1. Open a terminal in the project root.
2. Create a virtual environment and install dependencies:

```bash
cd backend
python -m venv .venv
. .venv\Scripts\activate
pip install -r requirements.txt
```

3. Configure your environment:

```bash
copy backend\.env.example backend\.env
```

Or edit the project root `.env` directly if you want the values to be read by the app.

Example configuration values:

```env
AI_PROVIDER=omniroute
OMNIROUTE_BASE_URL=http://localhost:20128/v1
OMNIROUTE_API_KEY=your_omniroute_key_here
OMNIROUTE_MODEL=auto/best-coding
TTS_ENABLED=true
ELEVENLABS_API_KEY=your_key_here
ELEVENLABS_VOICE_ID=CwhRBWXzGAHq8TQ4Fs17
WHISPER_MODEL_SIZE=small
STORAGE_ROOT=/app/storage
```

If `AI_PROVIDER=ollama`, the app falls back to the local Ollama endpoint instead of OmniRoute.

4. Start the API server:

```bash
cd backend
$env:PYTHONPATH='.'
python main.py
```

5. Start the worker:

```bash
cd backend
$env:PYTHONPATH='.'
python worker.py
```

6. Create a job or use the smoke runner:

```bash
cd backend
$env:PYTHONPATH='.'
python .\scripts\smoke_run_job.py "https://example.com/sample.mp4"
```

## Real job examples

This project works best with a direct media URL or a local file.

### Direct MP4 file

```bash
cd backend
$env:PYTHONPATH='.'
python -c "from database.session import SessionLocal, init_database; from database.models import Job; from worker import process_job; init_database(); db=SessionLocal(); job=Job(youtube_url='https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/720/Big_Buck_Bunny_720_10s_1MB.mp4', status='created'); db.add(job); db.commit(); db.refresh(job); print(job.id); db.close(); process_job(job.id)"
```

### YouTube URL

YouTube downloads can fail with 403/geo/rate-limit issues depending on the environment and network. The app still supports YouTube, but direct media URLs are typically the most reliable local option.

## Output location

Generated artifacts land under:

```text
storage/jobs/<job_id>/
```

Typical output:

- `source.mp4`
- `audio.wav`
- `segments.json`
- `words.json`
- `short_0.mp4`, `short_1.mp4`, ...
- `final_short_0.mp4`
- `voice_0.mp3` / `voice.mp3`
- `metadata.json`
- `thumbnail.jpg`
- `pipeline.log`

## TTS notes

When `TTS_ENABLED=true` and a valid `ELEVENLABS_API_KEY` is configured, the worker generates a voice track for each clip and muxes it into the final video. If TTS fails, the job continues without stopping the rest of the pipeline.

## Troubleshooting

- No audio track found: the pipeline creates a silent WAV fallback automatically.
- OmniRoute or Ollama unreachable: clip selection and metadata generation fall back to heuristics so the job can still finish.
- YouTube 403: try a direct MP4/URL or a different source video.
- Thumbnail generation failures: the pipeline warns and continues without a thumbnail.
- Fonts missing: default Pillow/OS fonts are used automatically.

## Production notes

- Keep API keys in environment variables or secret storage.
- Prefer direct media URLs or a hosted source in production when YouTube restrictions are a problem.
- Keep the worker and API services running separately for cleaner operational behavior.
