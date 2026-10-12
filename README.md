<div align="center">
  <h1>🎬 AI Shorts Generator</h1>
  <p><b>A local-first AI pipeline that turns long-form videos into viral vertical shorts.</b></p>
  
  [![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
  [![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-00a393.svg)](https://fastapi.tiangolo.com/)
  [![OpenAI](https://img.shields.io/badge/OmniRoute-Enabled-black.svg)]()
  [![Yolov8](https://img.shields.io/badge/YOLOv8-Tracking-yellow.svg)]()
</div>

<br/>

## ✨ Key Features

- 👤 **Dynamic Focus Tracking (YOLOv8):** Uses AI to track faces and objects. Intelligently performs split-screen camera changes ("one up, one down" panning) between multiple speakers.
- 🎨 **Cinematic Motion Titles:** Generates clean, professional text overlays with soft drop-shadows, fading/sliding animations, and custom aesthetic accents without generic gray background boxes.
- ✂️ **Smart AI Clip Selection:** Finds and extracts the most viral moments from long transcripts.
- 🎙️ **Audio & Caption Sync:** Auto-transcribes using local **Whisper**, generating accurate word-level dynamic captions (ASS).
- 🧹 **Clean Pipeline output:** Instead of littering your folder with 10 intermediate copies, it intelligently cleans up after itself and spits out one perfect `final_video.mp4` for you to use.
- 🔊 **Auto SFX & Enhancements:** Adds transitions, sound effects, metadata generation, and custom YouTube/TikTok descriptions.

---

## 🏗️ Architecture

- **`backend/`** — Built with FastAPI. Contains the worker, API, config, service layers, and tracking algorithms.
- **`storage/`** — SQLite databases (`app.db`) and user media (`storage/jobs/<job_id>`).
- **`n8n_workflows/`** — Automations to trigger pipelines externally.

---

## 🛠️ Requirements

Before flying, ensure you have:
1. **Python 3.11+** installed
2. **FFmpeg** configured on your `PATH`
3. An **OmniRoute** or **OpenAI-compatible** API running for LLM prompts 

---

## 🚀 Quick Start

### 1-Click Launch (Windows)

From the root project directory, run:
```powershell
./start_ai_shorts.ps1
```
*This script will auto-create your backend virtual env, install `pip` packages (including PyTorch, Ultralytics YOLO & Whisper), and start both the API & Worker.*

### Manual Terminal Setup

1. **Initialize the Virtual Environment**
   ```bash
   cd backend
   python -m venv .venv
   . .venv\Scripts\activate
   # Or on Mac/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**
   ```bash
   copy backend\.env.example backend\.env
   ```
   *Edit `.env` according to your specific setup (Example values):*
   ```env
   AI_PROVIDER=omniroute
   OMNIROUTE_BASE_URL=http://localhost:20128/v1
   OMNIROUTE_API_KEY=your_key_here
   STORAGE_ROOT=/app/storage
   ```

3. **Start the API Server** (in Terminal 1)
   ```powershell
   cd backend
   $env:PYTHONPATH='.'
   python main.py
   ```

4. **Start the AI Worker** (in Terminal 2)
   ```powershell
   cd backend
   $env:PYTHONPATH='.'
   python worker.py
   ```

---

## 📹 Generating a Short

You can either pass Jobs via the API/frontend, or test it programmatically. 
Direct media (MP4) URLs usually prove more reliable than YouTube links (due to IP blocks).

```powershell
cd backend
$env:PYTHONPATH='.'
python .\scripts\smoke_run_job.py "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/720/Big_Buck_Bunny_720_10s_1MB.mp4"
```

## 📁 Output Artifacts

Generated assets land directly inside: `storage/jobs/<job_id>/`

A successfully processed job keeps the directory clean. You will typically find:

- `source.mp4` (The raw video)
- `words.json` & `segments.json` (Word-level timings; `audio.wav` is deleted once
  the transcript is written - it is only ever an input to the transcriber)
- **`clip_X/`**
  - `metadata.json` (Social-ready descriptions)
  - `thumbnail.jpg` (Custom title thumbnail)
  - `storyboard.jpg`
  - **`final_video.mp4` (The processed, ready-to-upload Short)**
- `pipeline.log` (Internal event logs)

---

## 🧹 Storage maintenance

A finished job keeps the whole source video, so a single multi-hour upload can
sit on the disk at 15-20 GB. `prune_storage.py` reclaims that, and reports
before it deletes anything:

```bash
cd backend
python -m scripts.prune_storage                 # what would go, and why
python -m scripts.prune_storage --apply         # delete that report
python -m scripts.prune_storage --older-than-days 3 --keep-last 2
```

It only ever considers: interrupted-download fragments, `audio.wav` whose
transcript already exists, job directories with no database row, and the
`source.mp4` of jobs that finished more than `--older-than-days` ago (keeping
the newest `--keep-last`). Clips that were never uploaded are never touched;
uploaded ones only with `--include-uploaded-clips`.

The worker itself refuses to start a job while the volume has less than
`MIN_FREE_DISK_GB` free (default 10), and aborts a download that runs out of
room mid-flight, so a full disk queues work instead of corrupting the database.

---

## 📤 Auditing what was actually published

The `uploaded` flag and what YouTube has can drift apart. An old caller posted
the placeholder id `queued` to the status endpoint, which marked a clip published,
recorded no video, and took it off every pending list without anything reaching
YouTube; the endpoint now rejects that, but the rows survive. `reconcile_uploads.py`
reads the clip table and the files on disk and reports the mismatch, changing
nothing unless told to:

```bash
cd backend
python -m scripts.reconcile_uploads                      # audit every clip
python -m scripts.reconcile_uploads --verify             # also ask YouTube about each id
python -m scripts.reconcile_uploads --status placeholder_id
python -m scripts.reconcile_uploads --apply              # rewrite stale file paths
python -m scripts.reconcile_uploads --apply --reset-placeholders
```

Every clip lands in one bucket: `published`, `placeholder_id` / `no_video_recorded`
(flagged uploaded with no real id), `duplicate_id`, `id_without_flag`,
`id_not_on_youtube` (only with `--verify`), `uploadable`, `blocked_by_retries`,
or `nothing_to_upload`. `--apply` only repairs file pointers and, with
`--reset-placeholders`, clears the bogus uploaded flag so the clip returns to the
pending list. It never marks anything uploaded and never uploads anything -
publishing stays a human decision.

---

## ⚙️ Running more than one worker

Jobs are handed out with a compare-and-swap (`UPDATE ... WHERE status IN
('created','queued')`), so any number of worker processes can poll the same
database and exactly one of them wins each job. A worker that dies mid-job left
the row stuck at `processing` forever; a 60-second heartbeat refreshes its lease,
and `JOB_LEASE_MINUTES` (default 90) after the last heartbeat another worker
reclaims it. This is all tested in `backend/tests/test_worker_concurrency.py`.

```bash
# Local: every worker is just another poller over the same database
cd backend && python worker.py

# Docker: the worker service has no fixed container_name, so it scales
docker compose up -d --scale shorts-worker=2
```

Rendering is ffmpeg-bound and a single encode already uses every core, so N
concurrent jobs on one machine do not finish N times faster - they mostly wait
on each other. Cap the encoder when you scale: with 8 cores and 2 workers at 2
jobs each, set `WORKER_FFMPEG_THREADS=2` (in the shell or a `.env` next to
`docker-compose.yml`) so the renders split the machine instead of fighting over
it. `WORKER_MAX_JOBS` controls jobs-per-worker (arq concurrency) and defaults to
`2`.

Each worker process also loads its own Whisper and YOLO models - around 1 GB
resident just to transcribe with the `tiny` model, more with a larger one - so
the second worker costs memory as well as cores.

---

## 🐳 The Docker stack

`docker-compose.yml` starts four services on one bridge network: `shorts-api`
(the FastAPI app and the web UI), `shorts-worker` (the render pipeline), `n8n`
(optional automations) and `omniroute` (LLM gateway). Copy `.env.example` to
`.env` first - compose substitutes those variables when it builds the file, so
this is where Docker knobs live, as opposed to `backend/.env`, which configures
the app itself.

| Service | Inside the network | On your machine |
| --- | --- | --- |
| shorts-api | `http://shorts-api:5001` | `http://localhost:5002` (set `SHORTS_API_HOST_PORT` to move it) |
| n8n | `http://n8n:5678` | `http://localhost:5678` |
| omniroute | `http://omniroute:20128` | `http://localhost:3001` |

Container-to-container calls use the left column; the two files in
`n8n_workflows/` already do, so import them as they are rather than editing the
URLs. `health_check.py` talks to the published port, so point it wherever the
stack actually answers: `SHORTS_API_BASE_URL=http://localhost:5002 python
health_check.py`.

n8n webhook basic auth is off by default. Turn it on by setting
`N8N_BASIC_AUTH_USER` and `N8N_BASIC_AUTH_PASSWORD` in `.env` (and
`N8N_BASIC_AUTH_ACTIVE=true`) - the compose file deliberately has no fallback
password, because a committed `admin`/`admin123` is readable by everyone who can
read the repository. It guards the `/webhook` endpoints only; the n8n editor has
its own owner account, created in the browser the first time you open it.

---

## 🩺 Troubleshooting / FAQ

- **Wrong/Stale Subtitles on re-run?:** Fixed in v1.1. Changing the source repository URL natively purges old cached artifacts automatically.
- **Missing or Blank Video Screen:** Ffmpeg/Yolov8 bounding crop issues? Ensure Python's Path features correctly interpret your local drive letter.
- **YouTube 403 / Geoblocked:** Depending on network environments, `yt-dlp` might fail. Just feed a direct MP4 file URL.
- **No Fonts Found:** The pipeline falls back cleanly to OS-defaults (like `arial` or `liberation`), though injecting `Montserrat-ExtraBold` is advised for the best cinematic overlays.