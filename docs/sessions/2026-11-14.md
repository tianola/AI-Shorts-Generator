# AI Shorts Generator — Session Summary (2026-11-14)

## What this project is

**AI Shorts Generator** is a local-first pipeline that turns long-form videos into vertical YouTube Shorts / TikTok clips. It runs entirely on a single Windows machine (Ryzen 7 5700X, 64 GB RAM, RX 580 4 GB VRAM — no CUDA).

**Pipeline stages (8 steps):**
1. Download (yt-dlp + Node for EJS n-solver)
2. Audio extraction → Whisper local transcription (`faster-whisper` on CPU)
3. Speaker diarization (pyannote, gated models — needs HF token)
4. Clip selection (LLM-driven viral moment finder)
5. Scene attention — what the clip is *about* (keyword → LLM text → optional VLM vision)
6. Cinematic render (dynamic captions, motion titles, SFX, 9:16 crop via YOLO tracking)
7. **Publish queue** — scheduled upload via YouTube `publishAt` (channel cadence, rolling daily budget ~10)
8. Optional TikTok direct upload

**Stack:** FastAPI (`backend/`) + SQLite (`storage/app.db`) + NSSM Windows services (`ShortsBackend` for API, `ShortsWorker` for pipeline) + OmniRoute local LLM gateway (`http://localhost:20128/v1`) + optional Ollama local VLM (moondream).

**Storage:** `storage/jobs/<job_id>/` — each job keeps `source.mp4`, `words.json`, `segments.json`, and per-clip `final_video.mp4` + `thumbnail.jpg` + `metadata.json`.

---

## What was done in this session (3 commits, all local, nothing pushed)

### 1. `1f553ca` — Stop a disconnected channel from retiring clips on strikes
**Root cause:** Job 109 lost five clips overnight because Google refused the refresh token (`invalid_grant: Token has been expired or revoked`). The queue treated each refusal as a clip fault, charged 3 strikes per clip, cleared both `publish_due_at` and `scheduled_publish_at` — clips vanished forever from any queue.

**Fix:** Channel-level auth refusals (invalid_grant, token expired/revoked) are now **forgiven**:
- No strike charged (`upload_retry_count` stays 0)
- Clip stays queued with `publish_due_at = now + PUBLISH_RETRY_MINUTES`
- Release slot (`scheduled_publish_at`) preserved
- `job_error` logs the repair step: "Reconnect the channel in Settings → YouTube; only a person can do that"

**Tests added (5):** `test_publish_hygiene.py` — covers expired token, revoked token, release slot survival, outage announced, ordinary failure still charges strike.

### 2. `dc5ecd8` — Say why the YouTube channel is not connected
**Problem:** `GET /api/youtube/status` returned `authenticated: false` with no reason. The Settings panel guessed from `has_client_secrets` alone, so an operator whose token was revoked saw "Click to connect" without knowing Google had refused the saved auth.

**Fix:** `get_youtube_auth_status()` now returns a `reason` field for all three not-connected states:
- No `client_secrets.json` → "download it from Google Cloud Console, add in Settings → YouTube"
- Never authorized → "connect it in Settings → YouTube"
- Google refused saved token → "Google refused the saved authorization (...Google's words...). The refresh token is expired or revoked Google-side — reconnect in Settings → YouTube"

Both upload entry points (`upload_clip_to_youtube`, `upload_all_clips_for_job`) now raise `PermissionError` **with that reason** inside, so the ledger (`clip.upload_error`) captures Google's words, not a generic sentence. The batch path's old "not connected" wording was not recognised by `_is_channel_auth_failure`; now both entry points use a forgivable phrase.

**Tests added (3):** revoked token reports Google's words + repair step; never-authorized does not blame token; live connection has no reason.

### 3. `44fc20c` — Script-language gate at the publish boundary
**Trigger:** Clip 233 went live on 2026-09-17 with a Chinese title (`当他拿出这盘菌子的时候全场都愣住了`) on a Greek/English channel. Nothing in the pipeline checked the script of a published title/description/hashtag.

**Fix:** New function `_blocked_script(clip)` runs at the **single publish boundary** (inside `_ship_clip_to_youtube`, before the upload call). Allows:
- Greek (0x0370–0x03FF, 0x1F00–0x1FFF)
- Full Latin blocks (Basic, Extended-A/B/Additional/C/D, fullwidth, ligatures)

Any letter outside those ranges → clip is **held** (`status: held`), intent marker cleared, retry counter not incremented, log shows offending characters and: "Edit the copy and click again."

**Tests added (4):** Chinese title blocked; Greek title passes; Chinese hashtag blocked; log message names the characters + repair step.

---

## Evidence of live reload (both restarts via UAC + nssm)

| load | commit | ShortsWorker python | ShortsBackend python | git HEAD |
|------|--------|---------------------|----------------------|----------|
| 08:46 | `1f553ca` | 09:37:02 | 09:37:04 | `44fc20c 09:35:37` |
| 09:03 | `dc5ecd8` | — | — | — |
| 09:37 | `44fc20c` | 09:37:02 | 09:37:04 | `44fc20c 09:35:37` |

Both services show `Worker started. Polling for jobs...` in `storage/logs/app.log` after restart. `openapi.json` 200, 44 paths. `/api/youtube/status` returns the live `reason` field with Google's `invalid_grant` text.

---

## Current state (what's running now)

- **Suite:** 756 passed, 1 skipped, ruff (F,E9) clean
- **Tree:** clean, 3 new commits ahead of origin/master, **nothing pushed**
- **Services:** ShortsWorker + ShortsBackend running the latest code (`44fc20c`)
- **Channel:** still cannot publish — OAuth refresh token is dead Google-side. `GET /api/youtube/status` reports it honestly with `reason`.
- **Queue:** 0 clips waiting. One stale `publish_due_at` on clip 461 (already published, excluded by `youtube_video_id IS NULL` filter).
- **No upload, no credential touched, no DB write from agent.**

---

## What remains — **only the operator can do these**

| item | what it is | status |
|------|------------|--------|
| **Reconnect YouTube OAuth** | Settings → YouTube → approve consent screen. Live credential action — agent cannot do it. | ☐ operator |
| **Consent screen: Testing → Production** | If still in Testing mode, refresh tokens expire after 7 days. Publishing the app to Production in Google Cloud Console stops the cycle. Needs human browser access. | ☐ operator |
| **Job 109 clips 463-467** | Retired on 3 auth strikes each, clocks NULL. `auto_upload=1` on job 109 → a requeue publishes the moment auth returns. | ☐ operator decides |
| **Job 70 duplicate slots** | 6 video ids (214/232, 215/233, 216/234) — **all 6 return oEmbed 404 / yt-dlp "Video unavailable"**. Not "two live uploads to choose between" — they are dead rows (private or deleted). Only YouTube Studio can distinguish. | ☐ operator reviews in Studio |
| **Clip Virality Score** | Parked by ruling — choose fix Α (drop 50.0 floor) or Β (re-weight composite). `>= 90` tier unreachable at current max 88.8. | ☐ parked |
| **Growth playbook** | Sourced Shorts growth + monetization playbook — operator executes content levers. | ☐ parked |

---

## New finding recorded (needs a ruling)

**Script gate decision (Option Α implemented, Option Β follow-up):**
- Clip 233 published a Chinese title; census over 293 clips: 1 CJK title, so not systemic.
- Option **Α** (gate at publish boundary — done): catches every generator, operator can edit & re-click.
- Option **Β** (gate at generator): validate copy in `copy_generator`, fall through to next model in chain.
- Option **Γ** (prompt + language pin only): cheapest, least durable.
- **Recommendation:** Α now, Β as follow-up — the publish boundary is what every clip crosses.

Recorded in `TASKS - ai-shorts-generator.md` as an open decision item.

---

## Vault write-backs done

All notes updated in `E:\Documents\Obsidian Vault\AI-SECOND-BRAIN\`:
- `SESSIONS/2026-11-13 - Dead YouTube OAuth Token Retired Five Clips - Session.md` — follow-up block with both loads + new finding
- `PROJECTS/ai-shorts-generator/STATUS` — superseded "not loaded" line, appended 2026-11-14 block
- `PROJECTS/ai-shorts-generator/TASKS` — job 70 evidence appended, script-gate decision added, UAC bullet marked `[x]`
- `PROJECTS/ai-shorts-generator/ERRORS` — root cause entry for swallowed exception + wording defect
- `INDEX.md` — two dated bullets (code fix + two findings)

All `vault_edit.py` operations PASS: 0 control chars, 0 foreign glyphs introduced.

---

## How to continue (for another AI / cloud session)

1. **Load the repo:** `cd /e/01-AI-PROJECTS/ai-shorts-generator`
2. **Verify live state:** `NO_PROXY= no_proxy= .venv/Scripts/python.exe -m pytest -q --timeout=180` → 756 passed
3. **Check services:** `nssm status ShortsWorker` / `ShortsBackend` — child python PIDs should be recent
4. **Probe API:** `curl http://127.0.0.1:5001/api/youtube/status` — must show `reason` with `invalid_grant`
5. **Operator actions first:** Reconnect OAuth in the UI, then decide on the remaining items above.
6. **If you must write code:** Use TDD (`superpowers:test-driven-development`), never push, never upload, never touch credentials. The standing brief is in `project-ai-shorts-generator-standing-brief.md` (project memory).

---

## Key files to read next

| file | why |
|------|-----|
| `backend/worker.py` | all publish logic, queue drain, script gate (`_blocked_script`) |
| `backend/services/youtube_upload.py` | auth status + upload entry points with reason |
| `backend/tests/test_publish_hygiene.py` | all publish guards + script-gate tests |
| `backend/tests/test_youtube_upload.py` | auth status reason tests |
| `backend/static/js/youtube.js` | panel that reads `reason` and shows it |
| `backend/database/models.py` | Clip / Job schema (note: `index` needs quoting in SQL) |

---

## Standing rules (from project memory)

- **No push** — commits stay local
- **No upload / no publish / no credential** — agent brings the app live but never fires the real action
- **No secrets in repo** — `.env` stays local, vault notes say "configured in environment"
- **Greek prose only in chat** — scan for CJK/Cyrillic before sending
- **Vault write-back = reconcile** — never overwrite shared notes (`INDEX.md`, `GLOBAL/*`), append or patch one anchor
- **Every bug fix = test first** (TDD) — watch it fail, then implement

---

*Generated 2026-11-14. All commits local (`1f553ca`, `dc5ecd8`, `44fc20c`). Nothing pushed. Services live on `44fc20c`.*