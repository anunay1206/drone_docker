"""Celery tasks wrapping the pipeline.

Two jobs separated by the human-in-the-loop gate (see API_DESIGN.md §1):
  * ``job_a_analyze``  — Step 0 detect (once per ortho) -> Step 1 crop, features,
    cluster, k-analysis, t-SNE. Ends with the project in ``AWAITING_LABELS``.
  * ``job_b_finalize`` — Step 2 assign -> Step 3 validate (if ground truth) ->
    Step 4 KMZ export. Ends with the project ``COMPLETED``.

Heavy pipeline modules (torch, detectron2, detectree2, rasterio, ...) are
imported lazily *inside* the tasks so the FastAPI process and this module can be
imported without the ML stack installed.
"""
import csv
import os
import shutil
import sys
import traceback
from datetime import datetime

# Silence tqdm progress bars in the worker. Redirected to a file, tqdm's \r
# updates don't overwrite — every tick is saved, bloating run logs ~10x and
# drowning the real signal. tqdm reads TQDM_DISABLE at import, so this must run
# BEFORE the lazy pipeline imports (predict / tree_crown_pipeline / detectree2)
# inside the tasks. setdefault so an explicit env override still wins.
os.environ.setdefault("TQDM_DISABLE", "1")

from app.core.logging import get_logger, naive_now, with_context
from app.core.storage import ensure_project_dirs, project_paths, reset_dirs
from app.db import models
from app.db.session import SessionLocal
from app.services.pipeline_adapter import build_config
from app.workers.celery_app import celery_app

log = get_logger("app.pipeline")

# ── warm model caches (one per worker process) ─────────────────────────
_PREDICTORS: dict = {}
_DINOV2: dict = {}


class _Tee:
    """Write a stream to several sinks (console + per-job log file)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def _get_predictor(model_path: str, conf_threshold: float):
    import predict  # lazy

    key = (model_path, round(float(conf_threshold), 4))
    if key not in _PREDICTORS:
        _PREDICTORS[key] = predict.build_predictor(
            model_path, conf_threshold=conf_threshold
        )
    return _PREDICTORS[key]


def _get_dinov2(model_name: str, img_size: int):
    import tree_crown_pipeline as tcp  # lazy

    key = (model_name, img_size)
    if key not in _DINOV2:
        _DINOV2[key] = tcp.build_dinov2(model_name, img_size)
    return _DINOV2[key]


def _set_job(db, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.add(job)
    db.commit()


def _set_state(db, project, state, error=None):
    project.state = state
    if error is not None:
        project.error = error
    db.add(project)
    db.commit()


def _job_tracking_id(job, request_id: str | None) -> str | None:
    """Preserve an orchestrator idempotency key once the API has recorded it."""
    return job.celery_task_id or request_id


def _read_recommended_k(dir_cluster: str):
    path = os.path.join(dir_cluster, "k_recommendation_table.csv")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
        # table is sorted with rank 1 first
        return int(float(rows[0]["k"])) if rows else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# JOB A — detect + cluster  (ends at AWAITING_LABELS)
# ═══════════════════════════════════════════════════════════════════════
@celery_app.task(name="app.workers.tasks.job_a_analyze", bind=True)
def job_a_analyze(self, project_id: str, job_id: str):
    db = SessionLocal()
    logf = None
    project = db.get(models.Project, project_id)
    job = db.get(models.Job, job_id)
    # Bind correlation context INSIDE the task body: local-dispatch runs this in
    # a daemon thread and ContextVars don't inherit across threads.
    with with_context(
        job_id=job_id,
        project_id=project_id,
        dag_run_id=(getattr(job, "celery_task_id", None) or ""),
        stage="analyze",
    ):
      try:
        run = project.current_run or 1
        paths = ensure_project_dirs(project_id, run)
        # fresh analysis: drop any derived artifacts from a previous attempt of
        # THIS run so a stale feature/cluster cache cannot leak in. Sibling runs
        # (run_1, run_2, ...) are untouched.
        reset_dirs(
            project_id,
            ["detectree", "ortho", "polygons", "step1_output",
             "step2_output", "step3_output", "step4_output"],
            run,
        )

        logf = open(os.path.join(paths["logs"], "analyze.log"), "a", buffering=1)
        _set_job(db, job, state="RUNNING", started_at=naive_now(),
                 celery_task_id=_job_tracking_id(job, self.request.id),
                 log_path=logf.name)
        _set_state(db, project, "ANALYZING")

        cfg = build_config(project)

        with _redirect(logf):
            import predict
            import tree_crown_pipeline as tcp

            # ── Step 0: detection, once per uploaded ortho ──────────────
            _set_job(db, job, current_stage="detecting", progress=0.05)
            ortho_dir = paths["input_ortho"]
            stems = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(ortho_dir)
                if f.lower().endswith((".tif", ".tiff"))
            )
            if not stems:
                raise RuntimeError("No orthomosaics uploaded.")

            predictor = _get_predictor(cfg.DETECTREE_MODEL, cfg.CONF_THRESHOLD)
            for stem in stems:
                src_ortho = _find_ortho(ortho_dir, stem)
                det_out = os.path.join(paths["detectree"], stem)
                gj, _overlay, used = predict.run_detectree2_pipeline(
                    ortho_path=src_ortho,
                    predictor=predictor,
                    output_dir=det_out,
                    tile_size=cfg.TILE_SIZE,
                    buffer=cfg.BUFFER,
                    iou_threshold=cfg.IOU_THRESHOLD,
                    conf_threshold=cfg.CONF_THRESHOLD,
                )
                # feed Step 1: same-resolution ortho + per-ortho-prefixed polygons
                shutil.copy(used, os.path.join(paths["ortho"], f"{stem}.tif"))
                shutil.copy(gj, os.path.join(paths["polygons"], f"{stem}.geojson"))

            # ── Step 1: crop -> features -> cluster -> analyse -> t-SNE ─
            _set_job(db, job, current_stage="cropping", progress=0.35)
            crowns_dir = tcp.step1_crop_crowns(cfg)

            _set_job(db, job, current_stage="extracting_features", progress=0.50)
            model = _get_dinov2(cfg.MODEL_NAME, cfg.IMG_SIZE)
            X, names_df, _ = tcp.step1_extract_features(cfg, crowns_dir, model=model)

            _set_job(db, job, current_stage="clustering", progress=0.70)
            all_labels, inertia, sil, db_vals, dir_cluster = tcp.step1_cluster(
                cfg, X, names_df, crowns_dir
            )

            _set_job(db, job, current_stage="analyzing_k", progress=0.85)
            tcp.step1_analyze_k(cfg, inertia, sil, db_vals, dir_cluster)

            _set_job(db, job, current_stage="tsne", progress=0.92)
            tcp.step1_tsne(cfg, X, names_df, all_labels, dir_cluster)

        # surface k recommendation for the review step
        project.available_k = list(cfg.K_LIST)
        project.recommended_k = _read_recommended_k(dir_cluster)
        db.add(project)
        db.commit()

        _set_job(db, job, state="SUCCEEDED", current_stage="done",
                 progress=1.0, finished_at=naive_now())
        _set_state(db, project, "AWAITING_LABELS")

      except Exception as e:
        _fail(db, project_id, job_id, e)
        raise
      finally:
        if logf:
            logf.close()
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# JOB B — assign + validate + export  (ends at COMPLETED)
# ═══════════════════════════════════════════════════════════════════════
@celery_app.task(name="app.workers.tasks.job_b_finalize", bind=True)
def job_b_finalize(self, project_id: str, job_id: str):
    db = SessionLocal()
    logf = None
    project = db.get(models.Project, project_id)
    job = db.get(models.Job, job_id)
    # Bind correlation context INSIDE the task body (daemon-thread safe; see
    # job_a_analyze note).
    with with_context(
        job_id=job_id,
        project_id=project_id,
        dag_run_id=(getattr(job, "celery_task_id", None) or ""),
        stage="finalize",
    ):
      try:
        run = project.current_run or 1
        paths = ensure_project_dirs(project_id, run)
        # clean outputs from any previous finalize (e.g. after re-labeling) so
        # results never mix old and new species assignments.
        reset_dirs(project_id, ["step2_output", "step3_output", "step4_output"], run)

        logf = open(os.path.join(paths["logs"], "finalize.log"), "a", buffering=1)
        _set_job(db, job, state="RUNNING", started_at=naive_now(),
                 celery_task_id=_job_tracking_id(job, self.request.id),
                 log_path=logf.name)
        _set_state(db, project, "FINALIZING")

        cfg = build_config(project)
        labels = db.query(models.ClusterLabel).filter_by(project_id=project_id).all()
        if not labels:
            raise RuntimeError("No labels submitted.")
        cfg.CHOSEN_K = labels[0].chosen_k

        with _redirect(logf):
            import tree_crown_pipeline as tcp

            _set_job(db, job, current_stage="assigning", progress=0.20)
            tcp.step2_assign_species(cfg)

            if _has_ground_truth(cfg.GROUND_TRUTH_CSV):
                _set_job(db, job, current_stage="validating", progress=0.55)
                tcp.step3_validate(cfg)

            _set_job(db, job, current_stage="exporting", progress=0.85)
            tcp.step4_export_kmz(cfg)

            # Emit a STAC Item describing this run (footprint + params + assets).
            # Non-fatal: a STAC failure must not fail an otherwise-good run.
            _set_job(db, job, current_stage="stac", progress=0.95)
            try:
                from app.services.stac import write_stac_item

                write_stac_item(project, chosen_k=cfg.CHOSEN_K)
            except Exception:  # pragma: no cover - best effort
                log.warning("stac emission skipped", exc_info=True)

        _set_job(db, job, state="SUCCEEDED", current_stage="done",
                 progress=1.0, finished_at=naive_now())
        _set_state(db, project, "COMPLETED")

      except Exception as e:
        _fail(db, project_id, job_id, e)
        raise
      finally:
        if logf:
            logf.close()
        db.close()


# ── helpers ────────────────────────────────────────────────────────────
def _redirect(logf):
    from contextlib import redirect_stderr, redirect_stdout, ExitStack

    stack = ExitStack()
    stack.enter_context(redirect_stdout(_Tee(sys.__stdout__, logf)))
    stack.enter_context(redirect_stderr(_Tee(sys.__stderr__, logf)))
    return stack


def _find_ortho(ortho_dir: str, stem: str) -> str:
    for ext in (".tif", ".tiff"):
        cand = os.path.join(ortho_dir, stem + ext)
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError(f"Ortho for stem '{stem}' not found in {ortho_dir}")


def _has_ground_truth(gt_dir: str) -> bool:
    if not os.path.isdir(gt_dir):
        return False
    return any(
        os.path.isdir(os.path.join(gt_dir, d)) for d in os.listdir(gt_dir)
    )


def _fail(db, project_id: str, job_id: str, exc: Exception):
    tb = traceback.format_exc()
    job = db.get(models.Job, job_id)
    project = db.get(models.Project, project_id)
    stage = getattr(job, "current_stage", None) if job else None
    # Duration (ms) since the job started, if we have a start timestamp.
    duration_ms = None
    started_at = getattr(job, "started_at", None) if job else None
    if started_at is not None:
        try:
            duration_ms = int(
                (naive_now() - started_at).total_seconds() * 1000
            )
        except Exception:
            duration_ms = None
    # Durable, structured ERROR line (also lands in errors.jsonl) keyed by the
    # correlation ids already bound in the task body's with_context block.
    log.error(
        "job %s failed during stage=%s project=%s duration_ms=%s",
        job_id, stage, project_id, duration_ms,
        exc_info=exc,
    )
    if job:
        # Also append the traceback to the run's own .log file so the log is
        # self-sufficient for RCA — otherwise the file just stops mid-step and
        # the error lives only in the DB Job.error column.
        _write_failure_to_log(getattr(job, "log_path", None), job_id, exc, tb)
        _set_job(db, job, state="FAILED", error=tb, finished_at=naive_now())
    if project:
        _set_state(db, project, "FAILED", error=str(exc))


def _write_failure_to_log(log_path, job_id: str, exc: Exception, tb: str) -> None:
    """Append a clearly-marked failure block (timestamp + exception + traceback)
    to the run log. Best-effort: never raise from the failure path."""
    if not log_path:
        return
    try:
        from app.core.logging import now_ist
        ts = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
        with open(log_path, "a", buffering=1) as f:
            f.write(
                f"\n{'='*70}\n"
                f"ERROR  {ts}  job={job_id}\n"
                f"{type(exc).__name__}: {exc}\n"
                f"{'-'*70}\n{tb}\n{'='*70}\n"
            )
    except Exception:
        pass
