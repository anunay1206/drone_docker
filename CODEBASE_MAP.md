# Codebase Traversal Map

Purpose: quick lookup so a new session can jump straight to the right file instead of scanning the tree. Project = Tree-Crown Species Pipeline (drone orthomosaic -> crown detection -> clustering -> species labeling -> KMZ export), served as a FastAPI backend + static frontend + optional Airflow orchestration.

**How to search this file:** use Ctrl+F / Cmd+F. Look up a route in the [Endpoint Index](#endpoint-index), a concept in the [Keyword Index](#keyword-index), or a file in the tables below. All three are single-purpose tables so a text search lands on the right row directly.

**Keeping it current:** there's no git repo here, so change tracking is done by `update_codebase_map.py` — it snapshots file mtimes/sizes and logs what changed since the last run into [Recent Changes](#recent-changes). Run `python update_codebase_map.py` after a work session (or before starting a new one) to refresh that log. Everything else in this file is structural and only needs manual edits when the architecture itself changes (new endpoint, new service, new top-level folder).

## Recent Changes

<!-- RECENT-CHANGES:START -->

### 2026-07-03 18:12
- **Added:** none
- **Modified:** `.env`, `frontend\index.html`
- **Removed:** none

### 2026-07-03 17:48
- **Added:** `code\scripts\run_retention.py`
- **Modified:** `code\app\core\settings.py`, `code\app\core\storage.py`, `code\app\db\models.py`, `code\app\db\session.py`
- **Removed:** none

### 2026-07-03 13:51
- **Added:** `.gitattributes`, `docs\RETENTION_CONSENT_CLEANUP_PLAN.md`
- **Modified:** none
- **Removed:** none

### 2026-07-03 13:35
- **Added:** none
- **Modified:** `frontend\index.html`
- **Removed:** none

### 2026-07-03 11:44
- **Added:** `code\app\services\activity_log.py`, `docs\FRONTEND_REVIEW_LABEL_UX_PLAN.md`, `docs\LABEL_PARAMINFO_FAQ_PLAN.md`, `docs\OAUTH_GIS_INTEGRATION_PLAN.md`, `frontend\config.js`, `frontend\config.js.example`, `frontend\filebrowser_fetch_test.html`
- **Modified:** `.env.example`, `.gitignore`, `code\app\api\deps.py`, `code\app\core\settings.py`, `code\app\main.py`, `frontend\index.html`
- **Removed:** none

### 2026-07-02 10:48
- **Added:** `frontend\landing_samples.zip`, `frontend\landing_samples\landing_I_grid.html`, `frontend\landing_samples\landing_N_botanical_atlas.html`, `frontend\landing_samples\landing_S_observatory.html`, `frontend\landing_samples\landing_T2_cinematic.html`, `frontend\landing_samples\landing_T3_ribbon.html`, `frontend\landing_samples\landing_T4_fieldcards.html`, `frontend\landing_samples\landing_T_census.html`, `frontend\landing_samples\landing_U_watershed.html`
- **Modified:** none
- **Removed:** `frontend\landing_samples\landing_D_blueprint.html`, `frontend\landing_samples\landing_E_deco_canopy.html`, `frontend\landing_samples\landing_F_brutalist_glitch.html`

### 2026-07-02 01:29
- **Added:** `docs\LOGGING_IMPROVEMENTS.md`, `frontend\landing_samples\landing_A_field_journal.html`, `frontend\landing_samples\landing_B_aerial_survey.html`, `frontend\landing_samples\landing_C_canopy_minimal.html`, `frontend\landing_samples\landing_D_blueprint.html`, `frontend\landing_samples\landing_E_deco_canopy.html`, `frontend\landing_samples\landing_F_brutalist_glitch.html`
- **Modified:** `code\app\workers\tasks.py`, `frontend\index.html`
- **Removed:** none

### 2026-07-02 00:56
- **Added:** none
- **Modified:** none
- **Removed:** `update_drone_flow.zip`

### 2026-07-02 00:55
- **Added:** `update_drone_flow.zip`
- **Modified:** `frontend\index.html`
- **Removed:** none

### 2026-07-02 00:14
- **Added:** `airflow\dags\drone_analyze_dag.py`, `airflow\dags\drone_finalize_dag.py`, `code\app\__init__.py`, `code\app\api\__init__.py`, `code\app\api\deps.py`, `code\app\api\v1\__init__.py`, `code\app\api\v1\analyze.py`, `code\app\api\v1\clustering.py`, `code\app\api\v1\compute.py`, `code\app\api\v1\finalize.py`, `code\app\api\v1\labels.py`, `code\app\api\v1\projects.py`, `code\app\api\v1\results.py`, `code\app\api\v1\router.py`, `code\app\api\v1\runs.py`, `code\app\core\__init__.py`, `code\app\core\errors.py`, `code\app\core\models_registry.py`, `code\app\core\settings.py`, `code\app\core\storage.py`, `code\app\db\__init__.py`, `code\app\db\base.py`, `code\app\db\models.py`, `code\app\db\session.py`, `code\app\main.py`, `code\app\schemas\__init__.py`, `code\app\schemas\compute.py`, `code\app\schemas\job.py`, `code\app\schemas\labels.py`, `code\app\schemas\project.py`, ... (+30 more)
- **Modified:** `.env`, `.gitignore`
- **Removed:** `airflow/dags/drone_analyze_dag.py`, `airflow/dags/drone_finalize_dag.py`, `code/app/__init__.py`, `code/app/api/__init__.py`, `code/app/api/deps.py`, `code/app/api/v1/__init__.py`, `code/app/api/v1/analyze.py`, `code/app/api/v1/clustering.py`, `code/app/api/v1/compute.py`, `code/app/api/v1/finalize.py`, `code/app/api/v1/labels.py`, `code/app/api/v1/projects.py`, `code/app/api/v1/results.py`, `code/app/api/v1/router.py`, `code/app/api/v1/runs.py`, `code/app/core/__init__.py`, `code/app/core/errors.py`, `code/app/core/models_registry.py`, `code/app/core/settings.py`, `code/app/core/storage.py`, `code/app/db/__init__.py`, `code/app/db/base.py`, `code/app/db/models.py`, `code/app/db/session.py`, `code/app/main.py`, `code/app/schemas/__init__.py`, `code/app/schemas/compute.py`, `code/app/schemas/job.py`, `code/app/schemas/labels.py`, `code/app/schemas/project.py`, ... (+28 more)

### 2026-07-01 - feature work: STAC stage ids, overlay path, consent, past-runs (Airflow request UNCHANGED)
Backend + frontend only. Trigger conf to Airflow (`{project_id, job_id, run}`) and the DAG→backend request body/params are untouched — only compute **response** bodies and new human-facing endpoints changed. Plans: `docs/FEATURE_PLAN_SSO_CONSENT_STAC.md`, `docs/GOOGLE_OAUTH_PLAN.md`.
- **STAC id per stage** — `app/services/stac.py` `build_stac_item(...stage=)` appends `_analyze`/`_finalize` to `item.id`; threaded through `app/services/assets.py` (`stac_response`, `asset_response_fields`, `analyze_asset_fields`) and set at `app/api/v1/compute.py` `_ok(...stage=)`. `write_stac_item` tags `finalize`.
- **STAC footprint fallback** — `stac.py` new `_footprint_from_geojson()`; used when the ortho GeoTIFF has no embedded CRS so `geometry`/`bbox` (WGS84 lat/long) are never null in the finalize item.
- **Overlay link = storage-relative file path** — `app/api/v1/clustering.py` `detection_overlay_url` now `relative_artifact_path()` → `projects/<id>/work/run_<n>/detectree/S3C/overlay.png` (was an absolute API URL). `None` if file absent.
- **Consent capture** — `app/db/models.py` Project `consent` (0 no /1 all /2 unlabelled-only) + `consent_at`; auto-migrated in `app/db/session.py` `_migrate_sqlite_add_columns`. New `POST /project/consent` in `app/api/v1/results.py`; consent surfaced in results payload. Frontend consent box after finalize (`frontend/index.html`).
- **Past runs list** — new `GET /projects/mine` in `app/api/v1/projects.py` (per-user projects + FileBrowser `files_url` from `share_hash`). Frontend `#pastRuns` panel + `loadMyProjects()`.

### 2026-07-01 11:42 - baseline snapshot (reset after setup/testing)
- Traversal map + endpoint/keyword index + change-tracking script set up.

<!-- RECENT-CHANGES:END -->

## Top-level layout

| Path | What it is |
|---|---|
| `code/` | All backend Python source (FastAPI app + standalone pipeline scripts). |
| `frontend/` | Static web UI, single `index.html`, served by nginx (Dockerfile.frontend). |
| `airflow/dags/` | Two Airflow DAGs that call back into the backend's compute endpoints. |
| `docs/` | Architecture/integration/deploy docs (see table below). |
| `models/` | Detector `.pth` weight files (not in git; user-supplied). `models.yaml`-driven catalog. |
| `data/` | Runtime data: `treecrown.db` (sqlite), `storage/projects/<uuid>/` (per-project outputs), `hf-cache/` (HF/DINOv2 model cache). |
| `docker-compose.yml` / `docker-compose.hub.yml` | Compose stacks: build-from-source vs. pull-from-Docker-Hub. |
| `Dockerfile` / `Dockerfile.frontend` | Backend image / frontend (nginx) image. |
| `README.txt` | Docker Hub run instructions (end-user quickstart, ports 8123 api / 8200 ui). |
| `project_outline.md` | High-level project description. |
| `Tree-Crown-Flow.postman_collection.json` | Postman collection for exercising the API. |
| `sample_labels_k4.csv` | Example cluster->species label CSV (input to `/labels`). |

## Backend app (`code/app/`) — FastAPI

Entry point: `code/app/main.py` (creates `FastAPI()`, mounts `api_router`, `/livez` healthcheck, `init_db()` on startup).

| Path | Role |
|---|---|
| `app/api/v1/router.py` | Mounts all routers under `/api/v1`. Start here to find any endpoint. |
| `app/api/v1/projects.py` | Project lifecycle + GeoTIFF uploads. |
| `app/api/v1/runs.py` | **Frontend-facing** async run triggers (returns immediately, dispatches to Airflow/pipeline). |
| `app/api/v1/analyze.py` | Phase A compute callback: detection + clustering (sync, called by Airflow DAG `drone_analyze`). |
| `app/api/v1/clustering.py` | Human-in-the-loop review: t-SNE/k-selection, per-cluster thumbnails, crown images, detection overlay. |
| `app/api/v1/labels.py` | Upload filled cluster->species CSV; closes the gate between DAG 1 and DAG 2. |
| `app/api/v1/finalize.py` | Phase B compute callback: species assignment + validation + KMZ export (sync, called by Airflow DAG `drone_finalize`). |
| `app/api/v1/results.py` | Final results summary, downloads, per-run history/comparison. |
| `app/api/v1/compute.py` | STACD/Airflow compute callbacks, body-style, per algorithm node. |
| `app/api/deps.py` | Shared FastAPI dependencies. |
| `app/core/settings.py` | Env-driven settings (ports, Airflow URL, paths). |
| `app/core/storage.py` | Filesystem/project storage path helpers. |
| `app/core/models_registry.py` | Loads/parses `code/models.yaml` (detector + DINOv2 backbone catalogs). |
| `app/core/errors.py` | Structured `{"error": {...}}` exception handlers (installed in `main.py`). |
| `app/db/models.py` | SQLAlchemy models (sqlite, `data/treecrown.db`). |
| `app/db/session.py` | Engine/session + `init_db()`. |
| `app/db/base.py` | Declarative base. |
| `app/schemas/*.py` | Pydantic request/response schemas: `project.py`, `job.py`, `labels.py`, `compute.py`. |
| `app/services/airflow_client.py` | HTTP client to trigger Airflow DAG runs. |
| `app/services/run_dispatch.py` | Decides Airflow-vs-local-run dispatch for a triggered run. |
| `app/services/pipeline_adapter.py` | Adapter bridging API layer to `tree_crown_pipeline.py` functions. |
| `app/services/project_service.py` | Project CRUD/business logic. |
| `app/services/assets.py` | Asset (image/thumbnail/overlay) path resolution. |
| `app/services/filebrowser_client.py` | Talks to the FileBrowser sidecar for public share links (see `docs/filebrowser_*`). |
| `app/services/stac.py` | STAC-style item/catalog generation (largest service file, 310 lines). |
| `app/services/state.py` | Run/project state machine helpers. |
| `app/workers/celery_app.py` | Celery app config (if async workers used). |
| `app/workers/tasks.py` | Celery task definitions (largest worker file, 302 lines). |
| `app/workers/cleanup.py` | Cleanup/GC tasks for old runs/data. |

## Core pipeline scripts (`code/`, outside `app/`)

| Path | Role |
|---|---|
| `tree_crown_pipeline.py` | The actual CV/ML pipeline (972 lines) — Detectree2 detection -> DINOv2 features -> KMeans/t-SNE clustering -> species mapping -> KMZ export. Called by `pipeline_adapter.py` and/or the DAGs. |
| `predict.py` | Standalone detection/prediction script (284 lines). |
| `end_to_end_pipeline.py` | Script to run the whole pipeline outside the API (163 lines). |
| `config.py` | Plain-Python `Config` class: paths, tile/IOU/conf thresholds, K_LIST, EPSG, KMZ color palette — used by the standalone scripts, not the FastAPI app (which uses `app/core/settings.py` + `models.yaml` instead). |
| `models.yaml` | Model catalog: `detectors` (Detectree2 `.pth` weights, keyed by `model_key`) and `backbones` (DINOv2 timm models, keyed by `model_name`). |
| `predictions/` | Cached example prediction JSONs (tile-indexed, EPSG 32643). |
| `requirements.txt` / `requirements-api.txt` | Full pipeline deps vs. API-only deps. |

## Airflow (`airflow/dags/`)

| Path | Role |
|---|---|
| `drone_analyze_dag.py` | Single-node DAG: detection + feature extraction/clustering. Calls `POST {DRONE_API_BASE}/api/v1/compute/analyze`. |
| `drone_finalize_dag.py` | Calls the finalize compute endpoint (species assignment/validation/KMZ), mirrors analyze DAG structure. |

Both DAGs are triggered by our backend (`app/services/airflow_client.py`) via `POST {AIRFLOW}/api/v1/dags/<dag>/dagRuns`, and call back to `DRONE_API_BASE` (default `http://host.docker.internal:8123`).

## Docs (`docs/`)

| Path | Content |
|---|---|
| `INTEGRATION_GUIDE.md` | Full architecture + important files + Airflow integration + API call graph — **read this first for deep dives**. |
| `FRONTEND_BACKEND_FLOW.md` | What happens end-to-end when a UI button is clicked. |
| `README.md` / `docker_instruction.md` | Docker Hub deployment instructions (duplicates of top-level `README.txt`). |
| `filebrowser_documentation.md` | FileBrowser architecture/rationale (end-to-end). |
| `filebrowser_integration.md` | Why/how FileBrowser gives users a browsable output link post-finalize. |
| `filebrowser_public_share.md` | FileBrowser REST API for public share links. |
| `LOGGING_IMPROVEMENTS.md` | Run-log RCA plan — where logs live, what's done (#1 tqdm off, #4 traceback-in-log) + pending (#2 timestamps/levels, #3 header ids, #5 attempt separator, #6 level taxonomy, AuditLog). **Read before touching logging.** |
| `FEATURE_PLAN_SSO_CONSENT_STAC.md` | Plan for STAC stage ids, overlay path, consent, past-runs list — Airflow-request-unchanged. |
| `GOOGLE_OAUTH_PLAN.md` | Google SSO landing + per-user audit-log design (backend + frontend, Airflow untouched). |

## Endpoint Index

Every ro                                                                                                 