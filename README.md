# Tree-Crown backend — workstation Docker bundle

Self-contained bundle to run the backend (+ a simple frontend) in Docker on a
workstation, so an Airflow instance (local or remote) can trigger it.

The image is a **baked virtual-env + dependencies only**; the application code
and data/output are bind-mounted at run time. Detector model weights are kept
outside the Git repo and mounted from `HOST_MODELS_DIR`.

```
code/            backend code        -> mounted at /code   (the venv runs this)
data/            empty initially      -> mounted at /data   (storage/ + sqlite DB)
<model folder>   external weights     -> mounted at /models (read-only)
frontend/        simple static UI (its own nginx image)
airflow/dags/    the two DAGs, for whoever hosts Airflow
Dockerfile               backend image (baked venv, CPU torch by default)
Dockerfile.frontend      nginx serving frontend/
docker-compose.yml       api (8123) + frontend (8200)
.env.example             copy to .env and edit
```

## Ports

| Port | Who uses it |
|------|-------------|
| **8123** (host) → 8000 (container) | Airflow → backend `/api/v1/compute/*`; the browser/API base |
| **8200** (host) | the simple frontend |

The backend is one app on one port (8000 in the container). 8123 is just the
host publish of it. There is no separate "frontend port" on the API.

## One-time prep

1. Put the detector weights in any folder outside this repo, for example
   `/opt/treecrown-models` or `/tmp/treecrown-models`. The filenames must match
   `code/models.yaml`:
   `urban_trees_Cambridge_20230630.pth`, `220723_withParacouUAV.pth`,
   `230103_randresize_full.pth`.
2. `cp .env.example .env` and set `TCP_AIRFLOW_BASE_URL` (+ `TCP_AIRFLOW_PASSWORD`)
   to your Airflow.
3. Set `HOST_MODELS_DIR` in `.env` to the host folder containing the `.pth`
   files. The container always sees that folder as `/models`.

## Run

```
docker compose build          # first build is long (compiles detectron2)
docker compose up -d
docker compose logs -f api
```

- Frontend: http://localhost:8200  (set its "API base URL" to http://localhost:8123)
- API health: http://localhost:8123/livez
- API docs:   http://localhost:8123/docs

State persists in `./data` (sqlite + per-project storage). First analyze also
downloads the DINOv2 backbone into `./data/hf-cache` (needs internet once).

## Wiring Airflow (full loop)

The flow is: frontend → backend `/runs/analyze` → backend calls Airflow REST to
start the DAG → the DAG calls back to the backend `/compute/analyze` → backend
runs the pipeline.

On the machine running Airflow:

1. Install the two DAGs from `airflow/dags/` into the Airflow `dags/` folder.
2. Tell the DAGs where this backend is (env on the Airflow worker):
   ```
   DRONE_API_BASE=http://host.docker.internal:8123   # same machine
   # or  http://<workstation-ip>:8123                # different machine
   # DRONE_SERVICE_TOKEN=...   # only if TCP_COMPUTE_TOKEN is set on the backend
   ```
3. Make sure Airflow's REST API allows the backend's calls (basic-auth backend
   enabled) and that `TCP_AIRFLOW_BASE_URL` / credentials in `.env` match.

Reachability requirement: the Airflow host must be able to open **8123** to this
backend, and this backend must be able to open `TCP_AIRFLOW_BASE_URL`.

## Local mode (no Airflow)

Leave `TCP_AIRFLOW_BASE_URL` blank in `.env`: the trigger endpoints then run the
pipeline in-process and the frontend polls state exactly the same way. Useful to
verify the container works before involving Airflow.

## GPU build (optional)

CPU is the default. For NVIDIA/CUDA: build with
`TORCH_INDEX=https://download.pytorch.org/whl/cu118 docker compose build`,
switch the base image to a CUDA `python` image, run with `--gpus all`
(or compose `deploy.resources.reservations.devices`), and install
nvidia-container-toolkit on the host.

## Notes

- Editing code under `./code` takes effect on container restart (`docker compose
  restart api`) — it's mounted, not baked. Clear `__pycache__` if a stale `.pyc`
  shadows an edit.
- This bundle ships the API only (no Redis/worker/beat) — compute runs
  synchronously in the API process, which is all the Airflow trigger test needs.
- SQLite is fine for a single container; switch `TCP_DATABASE_URL` to Postgres
  for multi-replica.
