# Plan — consent-aware retention cleanup (standalone cron, no Celery)

## Decisions (this revision)
- **Keep detectree / ortho / polygons / crowns / clustering** for everyone who
  isn't fully deleted — the user may still want to view them.
- **Do not** depend on Celery workers / Beat being up → run as a **standalone
  script from system cron** with a file lock.
- **Size reduction is out of scope** for now.

## What already exists
`app/workers/cleanup.py` is a Celery Beat task that deletes every project older
than `retention_days` (DB row + folder), with a Redis lock and orphan sweep.
We are **not** using it as the driver (it needs Celery up, and it deletes
regardless of consent). Reuse its helpers (`delete_project_dir`) and its ideas
(per-project commit, orphan sweep) inside the new script.

> **Important:** if the Celery Beat task is currently scheduled, **disable it**
> (`cleanup_enabled=false`) before consent goes live — otherwise it wipes
> consented data at the old blanket cutoff.

## Behaviour by `Project.consent` (on expiry: `updated_at` older than window)
`consent` column already exists: 0 No / 1 Yes-all / 2 Yes-unlabelled-only.
- **0 (No)** → delete the whole project folder + DB row.
- **1 (Yes, all)** → **retain** — skip (kept for public training/viewing).
- **2 (Yes, unlabelled only)** → keep everything **through Step 1**; delete only
  the **labelled** outputs. Concretely:
  - **Delete:** `step2_output/` (species assignment), `step3_output/`
    (validation), `step4_output/` (KMZ species map).
  - **Keep:** `input/`, `detectree/`, `ortho/`, `polygons/`, `step1_output/`
    (crowns + clustering — cluster ids are unlabelled), `logs/`.
  - **DB:** delete the project's `ClusterLabel` rows (the species labels) and set
    `state="PRUNED"`; **keep** the Project + Ortho rows so the Step-1 data stays
    browsable. Stamp `pruned_at`.

"Older than a month, configurable" → `retention_days` default **30**.

## Changes required

### 1. Settings (`app/core/settings.py`) — small
```python
retention_days: int = 30                 # configurable window (was 7)
retain_consent_all: bool = True          # consent=1 → never auto-delete
cleanup_enabled: bool = False            # turn OFF the old Celery beat task
```

### 2. Storage helper (`app/core/storage.py`) — new, ~12 lines
`prune_labelled_outputs(project_id, run)`:
- delete `step2_output/`, `step3_output/`, `step4_output/` for that run
  (`shutil.rmtree(..., ignore_errors=True)` — idempotent).
- keep everything else. Nothing here touches detectree/ortho/polygons/crowns.

### 3. DB (`app/db/models.py` + `session.py`) — tiny
Add `pruned_at: DateTime | None` on `Project` (idempotency marker). Auto-migrated
by the existing `_migrate_sqlite_add_columns` (one line). Cascade already removes
`orthos`/`jobs`/`labels` on a Project delete.

### 4. Standalone retention script — new `scripts/run_retention.py` (~70 lines)
Self-contained; **no Celery/Redis**. What it does:
1. Acquire a **file lock** (`fcntl.flock` on e.g. `/data/storage/.retention.lock`,
   non-blocking) → exit if another run holds it. Auto-released when the process
   ends (even on crash/kill).
2. Open its own DB session (`SessionLocal`).
3. `cutoff = now - retention_days`; select projects with `updated_at < cutoff` and
   `state not in {ANALYZING, FINALIZING}`.
4. Per project, branch on `consent`:
   - `0` → `delete_project_dir(id)` + `db.delete(project)`.
   - `1` (and `retain_consent_all`) → skip.
   - `2` → skip if `pruned_at` already set; else `prune_labelled_outputs`,
     delete `ClusterLabel` rows, `state="PRUNED"`, `pruned_at=now`.
   - **commit after each project** (interrupt-safe: done ones stay done).
5. Optional **orphan sweep** (folder with no DB row past cutoff) — port from
   `cleanup.py`.
6. `--dry-run` flag: log what would happen, change nothing (use for first runs).
7. Print a summary (deleted / pruned / skipped counts) for the cron log.

### 5. Cron entry (ops, not code)
```
# daily 03:00 — adjust python/paths for your container/host
0 3 * * *  cd /code && python scripts/run_retention.py >> /data/storage/retention.log 2>&1
```
(Or run it inside the backend container via `docker exec` / a compose `cron`
sidecar. It only needs the app importable + the DB + storage volume — same env as
the API, no worker/broker.)

## Interrupt safety (single workstation)
- **File lock** (`flock`) → only one run at a time; released automatically on
  process exit, so a kill/crash never leaves a stuck lock (unlike a stale Redis
  key).
- **Per-project commit** → interruption leaves processed projects final; the rest
  are picked up on the next cron run.
- **Idempotent operations** → `rmtree(ignore_errors=True)`; `delete_project_dir`
  on a missing folder is a no-op; `consent=2` prune is skipped once `pruned_at` is
  set, so a half-done sweep just resumes and completes next run.
- **Never touches busy projects** (`ANALYZING`/`FINALIZING` excluded).

## Effort / risk
- **New:** `scripts/run_retention.py` (~70), `prune_labelled_outputs` (~12),
  `pruned_at` column. **Edited:** `settings.py` (3, incl. `cleanup_enabled=false`).
- **Risk:** low–moderate (destructive). Mitigate: `retention_days=30`, `--dry-run`
  first, test on a throwaway project, and disable the old Celery task so nothing
  double-deletes.
- **No Airflow / frontend change.** Existing `app/workers/cleanup.py` can stay in
  the tree (disabled) or be refactored to import the same shared function later.

## Order
1. `pruned_at` + settings (`cleanup_enabled=false`, `retention_days=30`).
2. `prune_labelled_outputs` + test on a copy.
3. `scripts/run_retention.py` with `--dry-run`; verify the summary on real data.
4. Add the cron entry; drop `--dry-run` once confident.
