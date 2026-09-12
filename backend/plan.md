# AI Shorts Generator Upgrades Plan

- [ ] **Setup:** Update `backend/requirements.txt` with new dependencies (instructor, mediapipe, arq, redis).
- [ ] **Config:** Update `backend/config.py` - set `WHISPER_MODEL_SIZE` to "tiny".
- [ ] **Worker:** Migrate to `arq` + `Redis` for task queue management (Replacing polling loop).
- [ ] **LLM:** Implement `instructor` for robust structured LLM outputs in `services/`.
- [ ] **Vision:** Replace `cv2` haarcascades with `mediapipe` in `services/vertical.py`.
- [ ] **Advanced Features:** (Diarization, B-roll, Programmatic Editing) - Add after foundation is stable.
- [ ] **Testing:** Run existing and new targeted tests after each major module conversion.
