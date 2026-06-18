"""Phase A — Detection + clustering (synchronous compute callback).

This is the Compute-service endpoint the orchestrator (Airflow DAG) calls. It
blocks until the pipeline finishes (or fails) and returns the review payload. An
internal Job row carries per-stage progress for logs/audit.

Idempotency (v4 §9.4): pass an ``Idempotency-Key`` header (the orchestrator's
dag_run_id). A repeat call with a key whose run already SUCCEEDED replays the
result without recomputing; a key whose run is still in flight returns 409
CONFLICT_BUSY.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key, require_service_token, resolve_project
from app.api.v1.clustering import build_clustering_payload
from app.api.v1.runs import _apply_run_config, _validate_trigger_body
from app.db import models
from app.db.session import get_db
from app.schemas.project import AnalyzeTrigger
from app.services.assets import analyze_asset_fields
from app.workers.tasks import job_a_analyze

router = APIRouter()

# Includes ANALYZING so the trigger (which already moved the project into the
# in-progress state) can hand off to this compute callback.
_ANALYZE_OK = {"UPLOADED", "ANALYZING", "AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED", "FAILED"}


def _analyze_payload(request: Request, project) -> dict:
    payload = build_clustering_payload(request, project)
    payload.update(analyze_asset_fields(project))
    return payload


@router.post("/project/drone_api")
def drone_api(
    request: Request,
    body: AnalyzeTrigger | None = None,
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not body or not body.project_id or not body.action:
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "project_id and action are required in the request body",
            "project_id": getattr(body, "project_id", None),
        })
    if body.action == "finalize":
        from app.api.v1.finalize import run_finalize
        project = resolve_project(db, user, body.project_id)
        return run_finalize(request, body, project, db, user, idempotency_key)
    if body.action != "analyze":
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "action must be 'analyze' or 'finalize'",
            "project_id": body.project_id,
        })
    project = resolve_project(db, user, body.project_id)
    return run_analyze(request, body, project, db, user, idempotency_key)


@router.post("/projects/{project_id}/analyze")
@router.post("/project/analyze")
def start_analyze(
    request: Request,
    body: AnalyzeTrigger | None = None,
    project=Depends(get_project),
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return run_analyze(request, body, project, db, user, idempotency_key)


def run_analyze(
    request: Request,
    body: AnalyzeTrigger | None,
    project,
    db: Session,
    user: str,
    idempotency_key: str | None = None,
):
    path_project_id = request.path_params.get("project_id")
    if not path_project_id and not (body and body.project_id):
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "project_id is required in the request body",
            "project_id": None,
        })
    if body and body.project_id:
        project = resolve_project(db, user, body.project_id)

    _validate_trigger_body(project, body)
    # Idempotent replay / in-flight guard.
    if idempotency_key:
        prior = (
            db.query(models.Job)
            .filter_by(project_id=project.id, celery_task_id=idempotency_key)
            .order_by(models.Job.started_at.desc())
            .first()
        )
        if prior and prior.state == "SUCCEEDED":
            db.refresh(project)
            return _analyze_payload(request, project)
        if prior and prior.state in ("QUEUED", "RUNNING"):
            raise HTTPException(409, {
                "code": "CONFLICT_BUSY",
                "message": "A run with this Idempotency-Key is already in progress",
                "project_id": project.id,
            })

    if project.state not in _ANALYZE_OK:
        raise HTTPException(409, {
            "code": "INVALID_STATE",
            "message": f"Cannot analyze from state {project.state}",
            "project_id": project.id,
        })
    if not project.orthos:
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "Upload at least one orthomosaic first",
            "project_id": project.id,
        })

    previous_state = project.state
    project.state = "ANALYZING"
    db.add(project)
    db.commit()
    db.refresh(project)
    _apply_run_config(db, project, body, previous_state)

    job = models.Job(
        project_id=project.id, type="analyze", state="RUNNING",
        started_at=datetime.utcnow(), celery_task_id=idempotency_key,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    project.error = None
    db.add(project)
    db.commit()

    try:
        job_a_analyze.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        # The task's _fail() already wrote FAILED state + the error tail.
        db.refresh(project)
        db.refresh(job)
        stage = job.current_stage or "unknown"
        if project.state == "ANALYZING":
            project.state = previous_state
            db.add(project)
            db.commit()
        raise HTTPException(500, {
            "code": "COMPUTE_FAILED",
            "message": project.error or str(exc),
            "project_id": project.id,
            "stage": stage,
        }) from exc

    db.refresh(project)
    return _analyze_payload(request, project)
