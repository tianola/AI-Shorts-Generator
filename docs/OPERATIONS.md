# Operator runbook — ai-shorts-generator

Written 2026-10-13 on this machine (Windows 11 `10.0.26300`, project on `E:`). Every
number below was measured, not estimated — they drift, so re-run the dry runs to refresh.

## 1. Where things live

| what | path |
|---|---|
| the only database | `storage/app.db` (SQLite, WAL mode) |
| per-job media | `storage/jobs/<job_id>/` — `source.mp4`, `clip_N/final_video.mp4`, `pipeline.log` |
| logs | `storage/logs/` |
| database backups | `storage/backups/app-<timestamp>.db` |
| ledgers of deleted rows | `storage/backups/stale-processing-jobs-<timestamp>.json` |
| secrets | `.env` — never printed, never committed |

`storage/` is ~59 GB and almost all of it is regenerable. **`app.db` is not**: it is the
only record of what was rendered and what reached YouTube. `storage/legacy_backend_storage`
is kept on purpose, do not clean it.

## 2. Command forms

Scripts are modules under `backend/scripts/`, run from `backend/` with the project venv
(Python 3.14.7, and it holds the dependencies):

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator\backend
../.venv/Scripts/python.exe -m scripts.prune_storage
```

Every script with a destructive option is **dry-run by default** and needs `--apply`.

Tests need only the venv and the in-memory database — `pytest.ini` already sets
`pythonpath = backend` and `testpaths = backend/tests`:

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator
set TEST_DATABASE_URL=sqlite:///:memory:
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

`TEST_DATABASE_URL` is not optional convenience: without it the suite writes into the
real `storage/app.db`. Baseline as of 2026-10-14 is **249 passed, 1 skipped**. A run from
`conftest.py` also defaults `APP_LOG_PATH` into the temp directory, because
`utils/logger.py` opens its rotating file sink at import time — without that, test errors
land in `storage/logs/app.log`, the file §9 reads to judge this install.

The two long-running processes start from `backend/` too; the modules import each other as
top-level names, so the repo root is not a working directory for them:

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator\backend
REM the API on http://localhost:5001
../.venv/Scripts/python.exe main.py
REM the pipeline, in its own window
../.venv/Scripts/python.exe worker.py
```

`python -m uvicorn backend.main:app` from the repo root does not work
(`ModuleNotFoundError: No module named 'utils'`). Host and port come from `.env`
(`API_HOST`, `API_PORT=5001`); the health route is `/api/health`.

`start_ai_shorts.ps1` starts both of those for you, and until 2026-10-15 it could not:
it looked for `backend\.venv\Scripts\AiShortsAPI.exe`, a console shim in a venv that is
not the one this project runs on. That venv existed with a full `site-packages`, but its
interpreter refused to start (`failed to locate pyvenv.cfg` on every command), so
`Test-Path` passed, `Start-Process` returned a PID, and the script printed
"AI Shorts Generator started" over two processes that were already dead. The launcher now
probes each candidate venv by *executing* it (`.venv` at the repo root first), starts
`python.exe main.py` / `python.exe worker.py`, and only reports success after
`/api/health` answers 200 — it exits 0 early if something already answers there.
`backend\.venv` is **gone** (deleted 2026-10-15, 1.9 GB; manifest
`storage/backups/deleted-backend-venv-manifest-20261015-095522.txt`) — nothing in the repo,
no scheduled task and no Startup entry resolved against it, and if it ever comes back it
must be through the root `.venv`, which is the install the tests and the running app use.

**`POST /api/jobs/{id}/restart` is not a dry run.** It wipes the job directory, re-downloads,
re-renders **and re-uploads** every clip if the job row carries `auto_upload = 1` — the flag
is stored per job, set when the job is created, and `upload_privacy NULL` falls back to
`AUTO_UPLOAD_PRIVACY` (default **unlisted**). Visibility maps across platforms
(`api/tiktok.py`): `unlisted` becomes TikTok `PUBLIC_TO_SELF`, which only the account owner
sees — it is not `PUBLIC_TO_EVERYONE`. Even so, a restarted job creates real objects on both
accounts and spends their daily upload quota. Check both columns before restarting an old
job on a live channel:

```bat
sqlite3 ..\storage\app.db "select id, status, auto_upload, upload_privacy from jobs where id=89;"
```

The dev tools come from the declared manifest, installed into `.venv` on 2026-10-13
(`pip install -r backend/requirements-dev.txt`). Lint is then the same rules CI runs:

```bat
.venv\Scripts\ruff.exe check backend --select F,E9
```

A declared-but-uninstalled **runtime** dependency does not always announce itself.
`google-api-python-client` (`backend/requirements.txt:39`) was missing from `.venv` on
2026-10-13 and `/api/youtube/status` answered `authenticated: false` — the channel looked
disconnected rather than broken, which silently blocked uploads and the view-count sync
while `No module named 'googleapiclient'` accumulated in `app.log`. If YouTube looks
unauthenticated but `client_secrets.json` is present, refresh the install before touching
the credentials:

```bat
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

## 3. Schema sync — applied 2026-10-13 21:25:09

The ten columns below were missing from `storage/app.db` while the models declared them.
The boot-time `_sync_columns()` adds them, additively — it never drops or rewrites
anything:

- `jobs`: `claimed_by`, `claimed_at`, `upload_privacy`
- `clips`: `score_breakdown`, `tiktok_video_id`, `hook_style`, `view_count`,
  `like_count`, `comment_count`, `stats_synced_at`

They are in the file now (`jobs` has 25 columns, `clips` 36), so
`python -m scripts.hook_style_report` and `GET /api/analytics/hook-styles` read normally.
Keep this section for the next time the models outrun the database; the symptom is the
same — `hook_style_report` prints *"Refusing to read: the clips table has no hook_style /
stats columns yet"* and the worker fills its log with `no such column: …`.

Both entry points run `init_database()` now. The API calls it from its lifespan and
`process_jobs()` calls it before its loop, so the worker may start before, after or
without the API. It used to assume the API had gone first: started a few seconds ahead on
2026-10-13 it polled against the missing columns and logged the same error 104 times,
because the loop survives a DB error and simply tries again.

`claimed_at` matters more than it looks. The worker's reclaim sweep requeues any job at
`status='processing'` whose lease expired **or is NULL**, so the first start after a sync
that adds it requeues every legacy row at `processing`. That is why the eleven stuck jobs
were deleted first (see §7). Check `SELECT count(*) FROM jobs WHERE status='processing'`
before a boot that adds a lease column.

## 4. Database: backup, verify, restore

Back up before any manual SQL, using the sqlite3 online backup API (the file is in WAL
and may be open by the backend — copying it with a file copy can capture a torn state):

```python
import sqlite3
src = sqlite3.connect('storage/app.db')
dst = sqlite3.connect('storage/backups/app-20261013.db')
src.backup(dst)          # consistent snapshot while writes continue
```

Verify, always before trusting a backup:

```python
sqlite3.connect(path).execute('PRAGMA integrity_check').fetchone()   # expect ('ok',)
```

Restore = stop the backend and the worker, put the file back at `storage/app.db`, delete
the `app.db-wal` / `app.db-shm` siblings that belong to the replaced database.

**Index corruption is possible here and is invisible.** The 2026-10-13 backup attempt
returned `wrong # of entries in index ix_jobs_id` / `row 31 missing from index
ix_jobs_id` — a row present in the table but absent from its own index, which makes
id-keyed reads silently miss it. `REINDEX` rebuilt it and integrity returned `ok`. Run
`PRAGMA integrity_check` occasionally; it is cheap and nothing else in the stack checks.

**Deleting job rows is a hand-run, ledger-first operation** (there is no UI for it). On
2026-10-15 the user asked for the old jobs gone: **61 of 78 `jobs` rows, 147 of 319 clips,
61 `job_progress` rows and 48 job directories (3.65 GB)** were deleted, and **17 jobs were
kept on purpose** — every job whose clips carry a live published `youtube_video_id` /
`tiktok_video_id` (those rows are the only map to what is on the channel, and job 70's are
what makes the duplicate-short deletion still possible), job 89 (the hook experiment's
arms), job 49 (the `queued` placeholder `reconcile_uploads` still owes), and job 90 because
it was in flight at that minute. Order that ran, in `storage/backups/delete_old_jobs_20261015.py`:
checkpoint WAL → `VACUUM INTO` backup → JSON ledger of every deleted row → **one**
`BEGIN IMMEDIATE` transaction deleting `job_progress`, `clips`, then `jobs` → rename each
directory to `<id>.deleting` before removing it (so a locked file cannot leave a
half-deleted job visible) → `integrity_check` + `foreign_key_check`. Artifacts:
`app-before-old-jobs-deleted-20261015-070050.sqlite`, `deleted-old-jobs-20261015-070050.json`.
After it: 17 jobs / 172 clips, integrity `ok`, 0 FK violations, `E:` **56.4 GB free**.
Do not script this against a live queue without the in-flight re-check — the script refuses.

## 5. Reclaim disk

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator\backend
REM report only - add --apply to delete exactly that report
../.venv/Scripts/python.exe -m scripts.prune_storage
../.venv/Scripts/python.exe -m scripts.prune_storage --apply
```

Measured 2026-10-13: **143 files, 37.1 GB** reclaimable — **and `--apply` was run the same
night (22:10) after the user approved it in plain terms.** The report was written to
`storage/backups/prune-plan-20261013-221001.txt` *before* deleting, and
`prune-applied-*.txt` records what went: 143 files, 37.1 GB. `E:` went from **17.5 GB free
to about 54 GB**. Categories the report gives:

- `[orphan]` — job directory with no job row and no clip rows (the deleted stuck jobs
  became exactly this; their ~4.9 GB is in the 37.1 GB)
- `[source]` — a finished job's full-length download (job 80's `source.mp4` is 17.2 GB)
- `[audio]` — `audio.wav` the transcriber already consumed
- `[partial]` — interrupted `yt-dlp` `.part` downloads

Guards: it skips queued/running jobs, and a clip that was never uploaded is never a
candidate — that file is the only copy of the output. `--include-uploaded-clips` opts in
to deleting rendered clips that are safely on YouTube; `--older-than-days` (default 7)
and `--keep-last` (default 2) bound how much of a job's output survives.

**The disk guards are all set to 10 GB and the volume runs nearly full.** On 2026-10-13 `E:`
had **17.5 GB free of 3726 GB** before the prune above took it to ~54 GB; the numbers here
describe the mechanism, which the reclaim did not change. Two checks exist, both against `MIN_FREE_DISK_GB`
(default **10**): `worker.py` refuses to claim a job without headroom (it sleeps 15 s and
asks again), and `services/youtube.py::yt_progress_hook` aborts a download once per
whole percent if free space drops below the floor. Nothing checks before the **render**
stage writes its outputs. So with 17.5 GB free a job is admitted, can consume ~7.5 GB of
source before the download guard trips, and then renders with no preflight at all — and
`job 80/source.mp4` alone is 17.2 GB. A volume that fills during a write is also the most
plausible way `ix_jobs_id` went corrupt: the same transaction that adds the job row adds
the index entry. Reclaim space with the report above when it runs low, and remember that
one long source video alone is tens of gigabytes; `df` on the volume before a long test
costs nothing.

`utils.disk.reclaim()` is a different thing: it deletes one finished job's
`audio.wav` / `audio_groq.mp3` after transcription. Useful, not a guard — it cannot free
space for a job that has not been claimed.

## 6. Reconcile the upload ledger

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator\backend
REM local audit, changes nothing
../.venv/Scripts/python.exe -m scripts.reconcile_uploads
REM same, plus asks YouTube about every recorded id
../.venv/Scripts/python.exe -m scripts.reconcile_uploads --verify
REM writes - only after the verify output has been read
../.venv/Scripts/python.exe -m scripts.reconcile_uploads --apply --reset-placeholders
```

The database claims 153 uploads; at least one row (`clip 169`, job 49) carries the
placeholder id `queued`, meaning the old status endpoint recorded a publish that never
happened. `--verify` is the read-only truth check against YouTube and is the one to run
first. `--apply` repairs file pointers and clears flags that were set on a lie; it never
marks a clip uploaded and never uploads anything. Not yet run with `--apply`.

## 7. Deleting rows: ledger first

Row deletes must export what they remove, because `clips` rows are the publish ledger.
The 2026-10-13 removal of the eleven legacy `processing` jobs did this: backup →
`REINDEX` → dump the exact rows to JSON → `--apply` → re-check integrity and orphans.

- Removed: 11 jobs, 33 clips (28 of them already uploaded), 11 `job_progress` rows.
- Ledger: `storage/backups/stale-processing-jobs-20261013-193802.json` (every column of
  every deleted row), plus the pre-delete database snapshot
  `storage/backups/app-20261013-193512.db`.
- The jobs' media directories were **left in place**; §5 is what deletes them.

There is no job-delete endpoint. Do not free-space a job by hand before deciding whether
its `clips` rows still need to survive.

## 8. Hook-style experiment

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator\backend
REM read-only table
../.venv/Scripts/python.exe -m scripts.hook_style_report
REM pulls YouTube statistics first, then prints
../.venv/Scripts/python.exe -m scripts.hook_style_report --sync
../.venv/Scripts/python.exe -m scripts.hook_style_report --json
```

Same thing over HTTP: `POST /api/analytics/sync-views`, `GET /api/analytics/hook-styles`.
`--sync` needs YouTube connected (`409` if not) and only reads statistics — nothing here
publishes. No arm has real view data yet; the table will be all `None` until clips from a
new job have run long enough to be counted.

## 9. Health check

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator
.venv\Scripts\python.exe health_check.py
.venv\Scripts\python.exe health_check.py --live
```

Default mode proves the API answers, a worker process exists, the log tail is clean, and
`POST /api/jobs` is mounted and validating — **without enqueuing work**. Creating a job
means downloading a source and rendering ~500 MB into `storage/jobs/`, and there is no
cancel endpoint to take it back, so that only happens with `--live`, which also requires
`SHORTS_HEALTH_URL` set to the video to process. `--live` reports whether the worker
claimed the job within 60s, which is the wiring proof; the render itself continues.

## 10. Logs: what is bounded

| file | writer | bound |
|---|---|---|
| `storage/logs/app.log` | `utils/logger` loguru sink | 20 MB rotation, 14 days retention |
| `storage/jobs/<id>/pipeline.log` | per-job loguru sink | 10 MB rotation, 14 days retention |
| `storage/logs/worker.log`, `worker_error.log` | `utils/daemon_streams` | `MAX_DAEMON_LOG_BYTES` = 20 MB, renamed to `.N` — but only when the daemon starts, so one long run can exceed the cap |
| `storage/logs/api.log`, `api.err.log`, `api_error.log` | `start_ai_shorts.ps1` `-RedirectStandardOutput` | **none** — truncated at each start, unbounded while running |

`storage/logs/app.log` is also a **signal, not just a log** — §9 tails it to decide
whether the install is healthy. `utils/logger.py` adds that sink at import time, which is
why `APP_LOG_PATH` exists: `backend/tests/conftest.py` points it into the temp directory
so a test run's deliberately-provoked errors never land here. If a Logs FAIL lists
`Access is denied: …\pytest-of-thana\…` paths, that was test noise rather than the app
(`0f8b1bc`, 2026-10-13) — the historical lines stay until rotation drops them.

`storage/logs/worker_error.log.gz` (9.9 MB, 2026-10-12) is not produced by anything in
this repo — no code path gzips a log. Someone compressed it by hand; it can be deleted
with the rest of the old logs.

Total `storage/logs` today: 16 MB, so nothing is urgent. The launcher's redirect files are
the only ones that can grow without limit during a long run; truncate them, not the
loguru sinks, if they ever get large.

## 11. Still a human decision

- ~~Run `prune_storage --apply` (37.1 GB). The volume has **17.5 GB free**; this is the
  cheapest way to make a long-video test safe (§5).~~ **Done 2026-10-13 22:10**, approved
  explicitly and ledged first — `E:` is at ~54 GB. The report is worth re-running
  periodically; the categories it flags reappear with every finished job.
- Decide `MIN_FREE_DISK_GB`: it stays at 10 GB while the largest source seen is 17.2 GB,
  and no stage after the download checks anything (§5). Raise it, or accept that a big job
  can render onto a full disk.
- Run `reconcile_uploads --verify`, then decide about `--apply --reset-placeholders`.
- **Duplicate shorts are live on YouTube.** Job 70 clips 0-2 each have two uploaded rows
  with six distinct video ids (`Lx1Eb12sWAs`/`ab5jGMdDxHA`, `Gp96VYc4WTE`/`VOq4SXTVFz8`,
  `IaMh66T2kR0`/`Uuj3zegIBNs`) — one re-run published them twice. The worker no longer
  creates the twin rows, but deleting the videos is a remote, public action.
- Jobs 15 and 17 still hold 16 duplicate `(job_id, index)` groups (job 15: nine, job 17:
  seven, three of those being triples) that were never uploaded. Harmless to the
  pipeline, but any per-index listing double-counts them.
- `API_HOST=0.0.0.0` with no authentication on the API (see the decision record); the
  publish safety item was explicitly deferred, so keep this machine off untrusted
  networks while the backend runs.
- The hook experiment has not been run — no arm has produced measurable views yet.
