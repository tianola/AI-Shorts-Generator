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
- `audio.wav` & `words.json` (Extraction data)
- **`clip_X/`**
  - `metadata.json` (Social-ready descriptions)
  - `thumbnail.jpg` (Custom title thumbnail)
  - `storyboard.jpg`
  - **`final_video.mp4` (The processed, ready-to-upload Short)**
- `pipeline.log` (Internal event logs)

---

## 🩺 Troubleshooting / FAQ

- **Wrong/Stale Subtitles on re-run?:** Fixed in v1.1. Changing the source repository URL natively purges old cached artifacts automatically.
- **Missing or Blank Video Screen:** Ffmpeg/Yolov8 bounding crop issues? Ensure Python's Path features correctly interpret your local drive letter.
- **YouTube 403 / Geoblocked:** Depending on network environments, `yt-dlp` might fail. Just feed a direct MP4 file URL.
- **No Fonts Found:** The pipeline falls back cleanly to OS-defaults (like `arial` or `liberation`), though injecting `Montserrat-ExtraBold` is advised for the best cinematic overlays.