# Logging Improvements — status + plan

Where run logs come from and how to make them RCA-grade. Refer here whenever we
touch logging. All changes are backend-only (no Airflow request/conf change).

## Where logs live today

- **Per-run pipeline logs:** `data/storage/projects/<project_id>/work/run_<n>/logs/analyze.log`
  and `finalize.log`. Written by `code/app/workers/tasks.py`:
  - `job_a_analyze` opens `analyze.log`, `job_b_finalize` opens `finalize.log`.
  - `_Tee` (tasks.py ~L31) fans stdout/stderr to console + the log file.
  - `_redirect(logf)` (tasks.py ~L270) installs the redirect for the pipeline body.
  - `Job.log_path` (DB, `app/db/models.py`) stores the file path.
- **Failure state:** `_fail()` (tasks.py) writes the full traceback to DB
  `Job.error` and `str(exc)` to `Project.error`.
- **Airflow side:** DAG prints `[compute] HTTP {status}: {text[:500]}` to the
  Airflow task log (separate system, truncated to 500 chars).

## Original assessment (2026-07)

Readable for a **successful** run (clear `STEP` banners, `✅` markers, species
distribution, file lists, KMZ path). Weak for **failure RCA**:

1. tqdm progress-bar spam — `\r` doesn't overwrite in a file, so every tick is
   saved. analyze.log ~50–66 KB, ~90% noise (`Predicting files ... it/s`).
2. No timestamps on any line — can't time steps or correlate to a request/DAG run.
3. No identifiers in the file — no project_id/job_id/dag_run_id/user header.
4. Traceback NOT in the log file — on crash the log stops mid-step; the error is
   only in DB `Job.error`. Must query DB to see the cause.
5. `finalize.log` duplicated — re-runs append to the same file with no
   separator/timestamp; can't tell attempts apart.
6. No level taxonomy on our own prints (`Step 4:`, `✅`); `grep error` false-hits
   on lines like `failed: 0`.

## DONE

### #1 — Silence tqdm in the worker  ✅
`code/app/workers/tasks.py`, top of module (before the lazy ML imports):
```python
os.environ.setdefault("TQDM_DISABLE", "1")
```
tqdm reads `TQDM_DISABLE` at import; setting it before `predict` /
`tree_crown_pipeline` / `detectree2` are imported inside the tasks disables all
progress bars → ~10x smaller logs. `setdefault` lets an explicit env override win.
Caveat: only affects calls that don't pass an explicit `disable=` kwarg (none of
the current pipeline code does).

### #4 — Write the traceback into the run log  ✅
`code/app/workers/tasks.py`, `_fail()` now calls `_write_failure_to_log(...)`
which appends a marked block to `Job.log_path`:
```
======================================================================
ERROR  <ISO-8601 UTC>  job=<job_id>
<ExceptionType>: <message>
----------------------------------------------------------------------
<full traceback>
======================================================================
```
Best-effort (never raises from the failure path). The log is now self-sufficient
for RCA — read the tail to see both the last step reached and the error.

## PENDING (next, in priority order)

### #2 — Timestamps + levels (biggest remaining win)
Replace the raw `open()` + stdout redirect with Python `logging` + a per-run
`FileHandler`, formatter `%(asctime)s %(levelname)s %(message)s`. Keep the same
file path so nothing downstream changes. Touch points: `job_a_analyze` /
`job_b_finalize` where `logf = open(...)` (tasks.py ~L127, ~L221) and `_redirect`.

### #3 — Run header block
First lines of each run log: `job_id, project_id, run, dag_run_id, user,
started_at`. One `logf.write(...)` right after the file is opened. Needs the
`dag_run_id` (available where the compute callback / dispatch runs) and `user`
(from the SSO session once OAuth lands — see `docs/GOOGLE_OAUTH_PLAN.md`).

### #5 — Attempt separator
On each run/attempt, write a banner `==== attempt @ <ts> ====` (or truncate the
file). Fixes the duplicate STEP-2/STEP-4 blocks in `finalize.log`.

### #6 — Level taxonomy on pipeline prints
Once #2 lands, route pipeline messages through the logger with real levels
(INFO/WARNING/ERROR) instead of bare `print`. Makes `grep ERROR` reliable.

### RCA-grade audit (bigger, tied to OAuth)
Operational/security RCA also needs (see `docs/GOOGLE_OAUTH_PLAN.md`):
- `AuditLog` table + HTTP middleware: `(ts, user_email, method, path,
  project_id, status, latency_ms, request_id, dag_run_id)`.
- A `request_id` per request, threaded into `Job` + pipeline log lines + STAC,
  and a stored `dag_run_id ↔ user ↔ project` map for cross-system trace.
- Ship logs (or the audit table) outside the per-project folder so the 7-day
  `retention_days` cleanup can't erase RCA data.
- Log successes at INFO too, not just failures.

## Files to touch (quick index)
- `code/app/workers/tasks.py` — log file open, `_Tee`, `_redirect`, `_fail`,
  `_write_failure_to_log`, task bodies (#1 #4 done; #2 #3 #5 #6 here).
- `code/app/db/models.py` — `Job.log_path`; future `AuditLog` model.
- `code/app/services/run_dispatch.py` / `app/api/v1/compute.py` — where
  `dag_run_id` is known (for #3 + audit correlation).
