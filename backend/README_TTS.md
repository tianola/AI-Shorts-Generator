ElevenLabs TTS setup

This project supports optional ElevenLabs text-to-speech generation for each short.

Required services
- Ollama (for AI clip selection and metadata generation) or a mock/fallback path.
- FFmpeg installed and available on PATH.
- Python environment with the backend dependencies installed.
- ElevenLabs API access when TTS is enabled.

How to enable
1. Copy `backend/.env.example` to `backend/.env` if you want a backend-local file, or add the values into the project root `.env`.
2. Set:
   - `TTS_ENABLED=true`
   - `ELEVENLABS_API_KEY=<your_key>`
   - `ELEVENLABS_VOICE_ID=alloy` (or another voice ID)
3. Restart the backend/worker process so the new environment is loaded.

Behavior
- The worker will generate a speaking voice track per clip and record it as `tts_path` in the clip metadata.
- If TTS generation fails, the job continues without blocking the rest of the pipeline.
- Costs depend on the number of clips and the ElevenLabs model/voice used.

Security
- Never commit real API keys to Git.
- Prefer environment secrets or a secret manager in production.

Quick smoke check
- With `TTS_ENABLED=true` and a valid key, run the job pipeline and check the generated job folder for `voice.mp3` files.
- If no key is available, leave `TTS_ENABLED=false` and the pipeline will skip TTS gracefully.
