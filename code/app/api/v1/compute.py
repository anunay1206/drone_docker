"""STACD/Airflow compute callbacks (body-style) — the endpoints the Airflow
framework calls per algorithm node.

Response contract (HTTP-status driven — the framework branches on the code):
  200  success, asset produced   -> Airflow task SUCCESS, dataset registered
       body: {"asset_id": "<path>", "version": "<n>", "hosting_platform": "..."}
  400  invalid input parameters  -> task SKIPPED (graceful), no dataset
  404  no data for these params  -> task SKIPPED (graceful), no dataset
  500  pipeline/computation fail -> task FAILED, DAG run fails
       error body (400/404/500): {"error": "<CODE>", "message": "<text>"}

These bodies are returned DIRECTLY (JSONResponse), bypassing the app's nested
``{"error":{...}}`` envelope, because the framework expects the flat shape above.

Idempotency: pass a stable ``Idempotency-Key`` header (the DAG's dag_run_id). A
repeat whose key already SUCCEEDED replays a 200 without recomputing. The key is
namespaced (``compute:<id>``) so it never collides with the trigger's placeholder
job (which stores the raw dag_run_id on its celery_task_id).
"""
import os

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import require_service_token
from app.core.storage import project_paths
from app.db import models
from app.db.session import get_db
from app.schemas.compute import ComputeRequest
from app.services.assets import asset_response_fields
from app.workers.tasks import job_a_analyze, job_b_finalize

router = APIRouter()

# Identifies which workstation produced the asset (returned on 200; env-overridable).
HOSTING_PLATFORM = os.getenv("TCP_HOSTING_PLATFORM", "act4dws4")

# Includes the in-progress state so the trigger (which already moved the project
# into ANALYZING/FINALIZING) can hand off to this compute callback.
_ANALYZE_OK = {"UPLOADED", "ANALYZING", "AWAITING_LABELS", "FAILED"}
_FINALIZE_OK = {"LABELS_SUBMITTED", "FINALIZING", "COMPLETED", "FAILED"}


def _ok(project, asset_id: str, version) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=asset_response_fields(project, asset_id, version),
    )


def _err(
    http_code: int, error: str, message: str, project_id: str | None = None
) -> JSONResponse:
    content = {"error": error, "message": message}
    if project_id:
        content["project_id"] = project_id
    return JSONResponse(status_code=http_code, content=content)


def _prior_job(db: Session, project, key: str | None):
    if not key:
        return None
    return (
        db.query(models.Job)
        .filter_by(project_id=project.id, celery_task_id=key)
        .order_by(models.Job.started_at.desc())
        .first()
    )


def _get_compute_project(db: Session, req: ComputeRequest):
    if req.project_id:
        return db.get(models.Project, req.project_id)
    return (
        db.query(models.Project)
        .filter_by(user_id="default")
        .order_by(models.Project.updated_at.desc(), models.Project.created_at.desc())
        .first()
    )


def _analyze_asset_id(project) -> str:
    """Path to the analyze output used as the STAC-D asset_id: the crown-polygon
    GeoJSON if present, else the Step-1 clustering output dir."""
    p = project_paths(project.id, project.current_run or 1)
    poly = p["polygons"]
    try:
        gj = sorted(f for f in os.listdir(poly) if f.lower().endswith(".geojson"))
        if gj:
            return os.path.join(poly, gj[0])
    except OSError:
        pass
    return p["step1_output"]


def _finalize_asset_id(project) -> str:
    """Path to the finalize output used as the asset_id: the species map KMZ."""
    p = project_paths(project.id, project.current_run or 1)
    return os.path.join(p["step4_output"], "species_map.kmz")


@router.post("/compute/analyze")
def compute_analyze(
    req: ComputeRequest,
    db: Session = Depends(get_db),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Single-node DAG A callback: Detectree2 detection + DINOv2/KMeans/t-SNE."""
    project = _get_compute_project(db, req)
    if not project:
        return _err(404, "NOT_FOUND", f"Project {req.project_id or 'latest'} not found", req.project_id)

    key = f"compute:{idempotency_key or req.execution_id}"
    prior = _prior_job(db, project, key)
    if prior and prior.state == "SUCCEEDED":
        return _ok(project, _analyze_asset_id(project), project.current_run or 1)
    if prior and prior.state in ("QUEUED", "RUNNING"):
        return _err(409, "CONFLICT_BUSY",
                    "A run with this Idempotency-Key is already in progress", project.id)

    if project.state not in _ANALYZE_OK:
        return _err(400, "INVALID_STATE", f"Cannot analyze from state {project.state}", project.id)
    if not project.orthos:
        return _err(400, "NO_INPUT", "No orthomosaic uploaded for this project", project.id)

    from datetime import datetime
    job = models.Job(project_id=project.id, type="analyze", state="RUNNING",
                     started_at=datetime.utcnow(), celery_task_id=key)
    db.add(job); db.commit(); db.refresh(job)
    project.state = "ANALYZING"; project.error = None
    db.add(project); db.commit()

    try:
        job_a_analyze.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        db.refresh(project)
        return _err(500, "COMPUTE_FAILED", project.error or str(exc), project.id)

    db.refresh(project)
    return _ok(project, _analyze_asset_id(project), project.current_run or 1)


@router.post("/compute/finalize")
def compute_finalize(
    req: ComputeRequest,
    db: Session = Depends(get_db),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Single-node DAG B callback: assign species + validate + reproject + KMZ."""
    project = _get_compute_project(db, req)
    if not project:
        return _err(404, "NOT_FOUND", f"Project {req.project_id or 'latest'} not found", req.project_id)

    key = f"compute:{idempotency_key or req.execution_id}"
    prior = _prior_job(db, project, key)
    if prior and prior.state == "SUCCEEDED":
        return _ok(project, _finalize_asset_id(project), project.current_run or 1)
    if prior and prior.state in ("QUEUED", "RUNNING"):
        return _err(409, "CONFLICT_BUSY",
                    "A run with this Idempotency-Key is already in progress", project.id)

    if project.state not in _FINALIZE_OK:
        return _err(400, "INVALID_STATE", f"Cannot finalize from state {project.state}", project.id)
    n_labels = db.query(models.ClusterLabel).filter_by(project_id=project.id).count()
    if n_labels == 0:
        return _err(400, "NO_LABELS", "No labels submitted for this project", project.id)

    from datetime import datetime
    job = models.Job(project_id=project.id, type="finalize", state="RUNNING",
                     started_at=datetime.utcnow(), celery_task_id=key)
    db.add(job); db.commit(); db.refresh(job)
    project.state = "FINALIZING"; project.error = None
    db.add(project); db.commit()

    try:
        job_b_finalize.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        db.refresh(project)
        return _err(500, "COMPUTE_FAILED", project.error or str(exc), project.id)

    db.refresh(project)
    return _ok(project, _finalize_asset_id(project), project.current_run or 1)
