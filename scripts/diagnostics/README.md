# Diagnostics

One-off scripts for poking at a local install. They are not tests and CI does
not run them. Several point at job 93 under `E:/01-AI-PROJECTS/...`; change the
path before you use them on another job.

| Script | What it checks |
|---|---|
| `check_gemini_vision.py` | The Gemini Vision key and SDK work on a real frame |
| `check_vlm_fallback.py` | VLM scene attention on one rendered clip |
| `diagnose_job93.py` | What each pipeline stage wrote for one job |
| `inspect_clips.py` | Clip start/end rows in the SQLite DB (run from the repo root) |

Run them from the repo root with the backend venv, for example
`backend\.venv\Scripts\python.exe scripts\diagnostics\diagnose_job93.py`.
