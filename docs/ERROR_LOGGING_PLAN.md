# Plan — proper error logging (Airflow contract untouched)

## Hard constraint (honored throughout)
No change to the Airflow contract either way:
- trigger conf the backend sends (`app/services/run_dispatch.py::_conf`, `app/services/airflow_client.py::trigger_dag/trigger_drone_dag`) — byte-identical.
- request bodies/params the DAG sends (`ComputeRequest`, `Idempotency-Key`).
- compute-callback RESPONSE bodies + status codes the backend returns (`app/api/v1/compute.py::_ok/_err`, `analyze.py` drone_api/drone_status). Unchanged (200/400/404/409/500).
All logging is side-channel only (stdout + files + DB). No logger call mutates a response, adds a header to a compute response, or changes a status. The one new response header (`X-Request-Id`) is added ONLY on non-compute human/frontend paths via a path guard.

## 1. Central logging — new `code/app/core/logging.py`
- `contextvars.ContextVar` for `request_id, job_id, project_id, dag_run_id, stage`; a `ContextFilter` injects them into every `LogRecord` (missing → `-`).
- Two formatters (settings-selected): `JsonFormatter` (one JSON/line incl. exc) + text `%(asctime)s %(levelname)s %(name)s [req= job= proj= dag= stage=] %(message)s`.
- `configure_logging(force=False)` (idempotent): root level from `settings.log_level`; `StreamHandler(stdout)`; `RotatingFileHandler(log_dir/app.log)`; dedicated `RotatingFileHandler(log_dir/errors.jsonl, level=ERROR, JSON)`; `logging.captureWarnings(True)`.
- Helpers: `get_logger`, `with_context(**kw)` ctx-manager, `new_request_id()` (uuid4 hex).
- Init calls (add only): `main.py` lifespan (before `init_db`); `workers/celery_app.py` top + `@worker_process_init.connect` (prefork forks drop handlers); `scripts/run_retention.py::main` after argparse.

## 2. Error capture at every failure surface
- **errors.py** — log in handlers: `_unhandled_exception_handler` → `log.exception(...)`; `_http_exception_handler`/`_api_error_handler` → ERROR for ≥500, WARNING for 4xx. Envelope bodies/status unchanged.
- **main.py `audit_requests`** — mint `request_id` (or read incoming `X-Request-Id`), push into ContextVar around `call_next`; set `X-Request-Id` header ONLY for `/api/` **non-compute** paths (guard `_COMPUTE_PATH_PREFIXES` excludes `/api/v1/compute`, drone_api/drone_status/analyze/finalize callbacks). Log every req at INFO (method,path,status,latency_ms,request_id); ≥500 also ERROR. Pass `request_id` into `activity_log.append(**extra)`.
- **tasks.py** — task bodies (`job_a_analyze`,`job_b_finalize`) push `with_context(job_id, project_id, dag_run_id=job.celery_task_id)` right after loading job (context binds inside the daemon thread — ContextVars don't inherit). `_fail` adds structured `log.error("job failed", exc_info, stage=job.current_stage)` (keeps `_write_failure_to_log`). STAC skip print → `log.warning(exc_info)`.
- **runs.py `_fail_trigger`** + dispatch `except RuntimeError` → `log.error(exc_info)` with project/job id (Airflow error text already in RuntimeError). 502 body identical.
- **compute.py / analyze.py / finalize.py** — `log.error("compute failed", exc_info)` BEFORE the unchanged `_err(500,"COMPUTE_FAILED",…)` / 500 raise. This is the key spot: the re-raised exception Airflow sees as a failed task now lands in `errors.jsonl` keyed by dag_run_id. Response untouched.
- **run_retention.py** — `_log`→logger; ERROR around per-project delete/prune (currently silent).
- **activity_log.py** — replace bare `except: pass` with `log.exception("activity_log write failed")` (still never raises into request path).

## 3. Correlation-id chain (side-channel)
HTTP `request_id` → `Job` row → per-run log → Airflow `dag_run_id`.
- `request_id` minted in middleware, returned via `X-Request-Id` (non-compute), written to ledger.
- **New nullable `Job.request_id`** column (`db/models.py`), populated at every Job-creation site (`runs.py::_new_job`, analyze/finalize/compute) by reading the ContextVar — nothing new crosses the Airflow boundary.
- `dag_run_id` is ALREADY stored side-channel on `Job.celery_task_id` — reuse it; logging just reads it into the `dag_run_id` field.
- Trace: user has `X-Request-Id` → grep `errors.jsonl`/`app.log` → get job/project/dag_run_id → open per-run `analyze.log`/`finalize.log`.

## 4. Levels / taxonomy (`grep ERROR` reliable)
Today false-positive: pipeline prints like `…failed: {n}` captured in run log. Fix: our emissions go through the logger with real levels; central formatter tags level → `grep ' ERROR '` / `jq 'select(.level=="ERROR")'` never matches a data line. Taxonomy: DEBUG (verbose), INFO (stage/req done), WARNING (recoverable: STAC skipped, GT missing, 4xx), ERROR (job failed, 5xx, dispatch failed). Converting standalone-script prints (`predict.py`, `tree_crown_pipeline.py`, `end_to_end_pipeline.py`) is optional/deferred (LOGGING_IMPROVEMENTS #6).

## 5. Persistence + retention durability
Tiers: DB (`Job.error` traceback + `Project.error`), per-run log (self-sufficient, item #4 done, but **under the project folder the retention script prunes → not durable**), central `app.log`+stdout, dedicated `errors.jsonl`.
**Recommendation:** default `log_dir = /data/logs` — OUTSIDE `storage_root/projects/**`. The retention script only touches `storage_root/projects/<id>` + orphan sweep, so `/data/logs/{app.log,errors.jsonl}` survive prune AND full delete. Durable RCA record = `errors.jsonl` + DB; per-run logs treated as ephemeral.

## 6. Config (`settings.py`, new block)
```
log_level: str = "INFO"          # TCP_LOG_LEVEL
log_dir: str = "/data/logs"      # TCP_LOG_DIR — OUTSIDE storage_root/projects
log_json: bool = False           # TCP_LOG_JSON — text dev / JSON prod
log_max_bytes: int = 10_000_000
log_backup_count: int = 5
```
Add to `.env.example`. Text default (dev readable), flip `TCP_LOG_JSON=true` in prod.

## 7. Files touched — Airflow confirmation
new `core/logging.py`; edit `settings.py`, `main.py` (compute paths excluded from header), `core/errors.py`, `db/models.py` (nullable `Job.request_id`), `workers/tasks.py`, `workers/celery_app.py`, `api/v1/runs.py`, `api/v1/compute.py` (**_ok/_err/status identical**), `api/v1/analyze.py`, `api/v1/finalize.py`, `services/activity_log.py`, `scripts/run_retention.py`, `.env.example`.
**NOT touched:** `run_dispatch.py`, `airflow_client.py`. Compute endpoints keep `require_service_token` + byte-identical bodies/status.

## 8. Effort / order / risks
Effort ~1.5–2.5 days. Order: (1) settings + `logging.py`; (2) wire `configure_logging` into main/celery/retention, verify stdout + `/data/logs`; (3) request_id mint + header (non-compute) + ledger; (4) `Job.request_id` column + migration + populate; (5) ERROR/WARNING at each surface; (6) context binding in task bodies + convert STAC/retention prints; (7) verify `grep ERROR`/`jq` clean + snapshot-test compute response bodies/headers/status unchanged.
Risks: prefork drops handlers → `worker_process_init` re-init; ContextVars don't cross into local-dispatch daemon thread → bind inside task body from the Job row (planned); existing-DB migration → nullable col + explicit `ALTER TABLE jobs ADD COLUMN request_id`; contract drift → path-guard + response snapshot test; disk → RotatingFileHandler caps + errors.jsonl ERROR-only.

---

## 9. Error logs ALSO in the logs folders (both tiers)
Every error is written to BOTH:
- **Per-run log** — `work/run_<n>/logs/{analyze,finalize}.log` (already via `_write_failure_to_log`; keep). Best for reading one run in context. Ephemeral (retention prunes it).
- **Durable central** — `/data/logs/app.log` (rotating, all levels) + `/data/logs/errors.jsonl` (ERROR-only, JSON, one object/line). OUTSIDE `storage_root/projects/**` → survives prune/delete.
So an auditor has the full run context (per-run log) AND a permanent queryable trail (`errors.jsonl`). The `_fail` path emits to all three (DB `Job.error`, run log, central) with the same correlation ids.

## 10. Verbose, classified error responses (human endpoints only)
**Constraint boundary:** the Airflow-facing compute callbacks (`compute.py::_ok/_err`, drone_api/drone_status) keep byte-identical flat bodies + status codes. Richer messages apply ONLY to the human/frontend endpoints (create/upload/from-url/labels/runs trigger/results) and to the **logs** (logs get full verbosity everywhere, including compute failures).

Every human-facing error returns the existing `{"error":{"code","message",...}}` envelope, but with a specific `code`, a plain-English `message` that names the exact problem, and a `hint` for remediation. Catalog to implement (add/normalize in `errors.py` + the raising sites):

| code | HTTP | message names the exact cause | hint |
|---|---|---|---|
| `MISSING_PARAM` | 422 | "Required field '<name>' is missing" — enumerate **every** missing field (from Pydantic `ValidationError.errors()`), not just the first | "provide: <fields>" |
| `INVALID_PARAM` | 400 | "'<name>'=<value> is invalid: <reason>" (e.g. `k=7` not in available_k `[2,4,6]`; `source_epsg` not an int; `conf_threshold` out of 0–1) | expected range/set |
| `NO_ORTHO` | 400 | "No orthomosaic uploaded for project <id>" | "upload a .tif first" |
| `NO_LABELS` | 400 | "No labels submitted for project <id>" | "submit the cluster table" |
| `INVALID_STATE` | 409 | "Cannot <action> from state <state> (allowed: <set>)" | next valid step |
| `CONFLICT_BUSY` | 409 | "A run is already in progress (state <state>)" | "wait for it to finish" |
| `UPLOAD_TOO_LARGE` | 413 | "File is <size>MB; limit is <max>MB" | — |
| `BAD_FORMAT` | 415 | "Expected a GeoTIFF (.tif/.tiff), got '<ext>'" | — |
| `DRIVE_FETCH_FAILED` | 502 | "Could not download from the Google Drive link: <reason>" (bad link / not public / HTTP <code>) | "make the link public" |
| `AIRFLOW_UNREACHABLE` | 502 | "Could not reach Airflow at <url>: <reason>" — classify `reason` from the exception: **connection refused**, **timed out after <n>s**, **DNS/name not resolved**, **no response** | "check the Airflow service / URL" |
| `AIRFLOW_HTTP_ERROR` | 502 | "Airflow returned HTTP <code> for DAG <dag>: <body[:500]>" | — |
| `FILEBROWSER_UNREACHABLE` | 502 | "FileBrowser unreachable at <url>: <reason>" (connection refused / timeout) | "check FileBrowser is up" |
| `FILEBROWSER_AUTH` | 502 | "FileBrowser login failed (HTTP <code>)" | "check FB credentials" |
| `FILEBROWSER_SHARE_FAILED` | 502 | "FileBrowser share API returned <code>: <detail>" | — |
| `COMPUTE_FAILED` | 500 | "Pipeline failed during <stage>: <exception type>: <msg>" (+ full traceback in logs) | "see run log / errors.jsonl by request_id" |
| `STORAGE_ERROR` | 500 | "Storage error: <ENOSPC disk full / permission denied>" | — |
| `UNAUTHENTICATED` | 401 | "Google sign-in required" / "invalid or missing token" | — |

**Connection-error classification helper** (new, in `core/errors.py` or `airflow_client`/`filebrowser_client`): map `urllib.error.URLError.reason` → `ConnectionRefusedError` = "connection refused", `socket.timeout`/`TimeoutError` = "timed out after <n>s", `socket.gaierror` = "host not found (DNS)", empty read = "no response received". `airflow_client.trigger_dag`/`get_dag_run_state` and `filebrowser_client` already raise `RuntimeError` with the base URL — extend them to attach the classified reason + a stable `code` so both the response and the log line say exactly which dependency is down and why. **This does not change what is sent to Airflow — only how the backend describes a *failed* outbound call.**

Each of these also logs an ERROR line with the same `code`, `message`, exception, and correlation ids.

## 11. "Who / when" — auditor fields on every error
Each failure produces a record (in `errors.jsonl`, the activity ledger, and mirrored to the run log) containing:
- **who** — `user_email`, `user_id` (from `X-User-Email`/`X-User-Id`; `"anonymous"` if unauth).
- **when triggered** — `triggered_at` (`Job.started_at`, ISO-8601 UTC).
- **when failed** — `failed_at` (error timestamp / `Job.finished_at`).
- **duration** — `duration_ms` (failed − triggered).
- **what** — `action` (analyze/finalize/upload/labels/…), `stage` (Job.current_stage), `project_id`, `run`, `code`, `message`, `http_status`, `dag_run_id`.
- **trace** — `request_id`, `client_ip` (from `X-Forwarded-For`/peer).

So an auditor answers "who ran what, when it started, when/why it failed" from one `errors.jsonl` line, then opens the per-run log by `request_id`/`dag_run_id` for the full traceback. Populate `who`/`client_ip` in the audit middleware (already reads the user headers); `triggered_at`/`failed_at`/`duration_ms` from the `Job` row in `_fail`; the rest from context.

**Airflow note (reaffirmed):** none of §9–11 alters the trigger conf, the DAG→backend request, or the compute-callback response bodies/status. Verbose text is confined to human endpoints + logs; the classified connection reasons describe *failed outbound calls*, not the payloads.
