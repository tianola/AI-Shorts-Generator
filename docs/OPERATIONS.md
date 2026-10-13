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
real `storage/app.db`. Baseline as of 2026-10-13 is **244 passed, 1 skipped**.

The dev tools come from the declared manifest, installed into `.venv` on 2026-10-13
(`pip install -r backend/requirements-dev.txt`). Lint is then the same rules CI runs:

```bat
.venv\Scripts\ruff.exe check backend --select F,E9
```

## 3. First start after 2026-10-13 — schema sync

`storage/app.db` is behind the models by ten columns. The boot-time `_sync_columns()`
adds them, additively — it never drops or rewrites anything:

- `jobs`: `claimed_by`, `claimed_at`, `upload_privacy`
- `clips`: `score_breakdown`, `tiktok_video_id`, `hook_style`, `view_count`,
  `like_count`, `comment_count`, `stats_synced_at`

Until that start happens, anything reading the experiment columns refuses and says so
(`python -m scripts.hook_style_report` prints *"Refusing to read: the clips table has no
hook_style / stats columns yet"*).

`claimed_at` matters more than it looks. The worker's reclaim sweep requeues any job at
`status='processing'` whose lease expired **or is NULL**, so the first start after the
sync would have requeued the eleven legacy stuck jobs and re-downloaded and re-rendered
them. They were deleted on 2026-10-13 (see §7), so a clean start now adds the columns and
requeues nothing.

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

## 5. Reclaim disk

```bat
cd /d E:\01-AI-PROJECTS\ai-shorts-generator\backend
REM report only - add --apply to delete exactly that report
../.venv/Scripts/python.exe -m scripts.prune_storage
../.venv/Scripts/python.exe -m scripts.prune_storage --apply
```

Measured 2026-10-13: **143 files, 37.1 GB** reclaimable. Categories it reports:

- `[orphan]` — job directory with no job row and no clip rows (the deleted stuck jobs
  became exactly this; their ~4.9 GB is in the 37.1 GB)
- `[source]` — a finished job's full-length download (job 80's `source.mp4` is 17.2 GB)
- `[audio]` — `audio.wav` the transcriber already consumed
- `[partial]` — interrupted `yt-dlp` `.part` downloads

Guards: it skips queued/running jobs, and a clip that was never uploaded is never a
candidate — that file is the only copy of the output. `--include-uploaded-clips` opts in
to deleting rendered clips that are safely on YouTube; `--older-than-days` (default 7)
and `--keep-last` (default 2) bound how much of a job's output survives.

The worker also protects itself: `utils.disk.has_headroom` / `reclaim` run before a
download so a full disk cannot start a job.

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

`storage/logs/worker_error.log.gz` (9.9 MB, 2026-10-12) is not produced by anything in
this repo — no code path gzips a log. Someone compressed it by hand; it can be deleted
with the rest of the old logs.
| `storage/logs/api.log`, `api.err.log`, `api_error.log` | `start_ai_shorts.ps1` `-RedirectStandardOutput` | **none** — truncated at each start, unbounded while running |

Total `storage/logs` today: 16 MB, so nothing is urgent. The launcher's redirect files are
the only ones that can grow without limit during a long run; truncate them, not the
loguru sinks, if they ever get large.

## 11. Still a human decision

- Run `prune_storage --apply` (37.1 GB).
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
