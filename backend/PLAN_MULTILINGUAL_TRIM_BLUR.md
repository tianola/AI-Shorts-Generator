# Multilingual, Precision Trim, and Smart Blur Implementation Plan (Task t_fedd10cc)

## 1. Multilingual Support & Translation
- **Goal**: Enable transcription AND translation.
- **Service (`backend/services/whisper.py`)**: Update `transcribe_audio_file` to accept an optional `task` parameter (default: "transcribe"). Pass this to `_transcribe_with_local_whisper` and `_transcribe_with_groq`.
- **Worker (`backend/worker.py`)**: Pass `task` from `Job` model (needs schema update if `Job` doesn't have it, or look for override).

## 2. Precision Trim Implementation
- **Goal**: Ensure functional and precise trim tools.
- **Review**: `backend/worker.py` `cut_clip` uses `-ss` and `-t`. This is functional. Check if it needs better boundary handling.

## 3. Content Safety (Smart Blur)
- **Goal**: Object-tracking blur for unsafe content.
- **Service (`backend/services/vertical.py` / `services/focus_tracking.py`)**:
    - Extend `detect_objects_in_frame` to also detect "unsafe" content (need to see if current YOLO model supports this, or if we define a list of unsafe classes). If YOLO model doesn't support specific unsafe classes, might need a different approach. Assuming we work with current model classes first (e.g., people, etc.), but instruction implies 'unsafe'. Need to investigate what 'unsafe' means in this context.
    - Implement FFmpeg filter chain for tracking blur: `boxblur` + `overlay` with tracking data (similar to how vertical crop tracking works).

## Implementation Steps
1.  **Draft Whisper.py updates**.
2.  **Verify precision of `cut_clip`**.
3.  **Investigate adding smart blur**.
