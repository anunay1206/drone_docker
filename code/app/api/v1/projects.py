"""Project lifecycle + uploads."""
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key
from app.core.models_registry import (
    DEFAULT_MODEL_KEY,
    list_backbones,
    list_models,
    resolve_backbone,
    resolve_model_path,
)
from app.core.settings import settings
from app.core.storage import delete_project_dir, ensure_project_dirs
from app.db import models
from app.db.session import get_db
from app.schemas.project import OrthoFromUrl, ProjectCreate, ProjectUpdate
from app.services.project_service import (
    USED_RUN_STATES,
    archive_current_run,
    serialize_project,
)
from app.services.state import transition_if

router = APIRouter()

# Transient states used purely as mutual-exclusion claims. Neither is a valid
# launch state for analyze/finalize, so holding one locks a run out for the
# duration of the request; both are released (or the row deleted) before it
# returns. See db/models.py for the durable state machine.
_UPLOADING = "UPLOADING"
_DELETING = "DELETING"
_BUSY_STATES = ("ANALYZING", "FINALIZING", _UPLOADING, _DELETING)
_DELETABLE_STATES = {
    "CREATED", "UPLOADED", "AWAITING_LABELS", "LABELS_SUBMITTED",
    "COMPLETED", "FAILED",
}


@router.get("/projects/mine")
def my_projects(
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
):
    """List the signed-in user's projects with their public FileBrowser share URL.

    Powers the landing page "your past runs" list. ``user`` is the API-key/SSO
    identity (single-tenant ``"default"`` until Google SSO lands, then the email).
    """
    from app.services.filebrowser_client import filebrowser_enabled, share_url

    fb_on = filebrowser_enabled()
    rows = (
        db.query(models.Project)
        .filter_by(user_id=user)
        .order_by(models.Project.updated_at.desc(), models.Project.created_at.desc())
        .all()
    )
    out = []
    for p in rows:
        hash_ = getattr(p, "share_hash", None)
        out.append({
            "project_id": p.id,
            "name": p.name,
            "state": p.state,
            "run_name": getattr(p, "run_name", None),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "files_url": share_url(hash_) if (fb_on and hash_) else None,
        })
    return {"projects": out}


@router.get("/detectors")
def get_detectors(user: str = Depends(require_api_key)):
    """List the registered Detectree2 detector weight files (+ availability/default)."""
    return list_models()


@router.get("/feature-extractors")
def get_feature_extractors(user: str = Depends(require_api_key)):
    """List the allowed DINOv2 feature-extractor models (valid model_name values)."""
    return list_backbones()


@router.post("/projects", status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
):
    model_key = body.model_key or DEFAULT_MODEL_KEY
    try:
        resolve_model_path(model_key)
        # Validate the feature-extraction backbone against the allowlist so a bad
        # model_name fails here rather than deep in the worker (Step 1B).
        body.params.model_name = resolve_backbone(body.params.model_name)
    except ValueError as e:
        raise HTTPException(400, {"code": "BAD_REQUEST", "message": str(e)})

    project = models.Project(
        user_id=user,
        name=body.name,
        model_key=model_key,
        source_epsg=body.source_epsg,
        params=body.params.model_dump(),
        state="CREATED",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_dirs(project.id, project.current_run)

    # Create a permanent FileBrowser share for this project's output folder.
    # Failure is non-fatal — project creation succeeds either way.
    try:
        from app.services.filebrowser_client import create_project_share, filebrowser_enabled
        if filebrowser_enabled():
            project.share_hash = create_project_share(project.id)
            db.add(project)
            db.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("FileBrowser share creation failed: %s", exc)

    return serialize_project(project)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), user: str = Depends(require_api_key)):
    rows = (
        db.query(models.Project)
        .filter_by(user_id=user)
        .order_by(models.Project.created_at.desc())
        .all()
    )
    return [serialize_project(p) for p in rows]


@router.get("/projects/{project_id}")
@router.get("/project")
def get_one(project=Depends(get_project)):
    return serialize_project(project)


# Dataset lock (v5): the input dataset (orthomosaic + ground truth) is editable
# ONLY during initial setup — before the project's first analysis. Once any run
# has been analyzed (or a re-run has been opened) the dataset is frozen for the
# life of the project. A re-run changes parameters + labels only, never the
# dataset; a different dataset means a NEW project.
def _assert_ortho_unlocked(project) -> None:
    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": "Cannot change the dataset while a run is in progress",
            "project_id": project.id,
        })
    # A bumped run counter or any archived run history means this project has
    # already been analyzed at least once -> dataset is frozen even though a
    # freshly-opened re-run sits in UPLOADED.
    has_prior_run = (project.current_run or 1) > 1 or bool(project.runs)
    if project.state not in ("CREATED", "UPLOADED") or has_prior_run:
        raise HTTPException(423, {
            "code": "ORTHO_LOCKED",
            "message": (
                "The input dataset (orthomosaic + ground truth) is locked because "
                "this project has already been analyzed. Re-runs change parameters "
                "and labels only — start a new project to use a different dataset."
            ),
            "project_id": project.id,
        })


@contextmanager
def _dataset_edit_lock(db: Session, project):
    """Hold an exclusive claim on the project while its input files change.

    ``_assert_ortho_unlocked`` alone is check-then-act: an analyze trigger can
    win the gap between it and the write, and then the job reads an ortho that
    ``_clear_existing_orthos`` is deleting out from under it. Two simultaneous
    uploads race the same way.

    Claiming the transient ``UPLOADING`` state with a single conditional UPDATE
    closes both: it is not a launch state, so analyze is locked out, and the
    loser of two concurrent uploads sees the claim fail. Released on exit — to
    ``CREATED`` if the project ended up with no ortho (the previous one was
    cleared before the write failed), otherwise back to where it started.
    ``_register_ortho`` advances to UPLOADED inside the body, in which case the
    release is a no-op because the state no longer matches.
    """
    pre_state = project.state
    if not transition_if(db, project, {pre_state}, _UPLOADING):
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": f"Project is busy (state {project.state}); try again",
            "project_id": project.id,
        })
    try:
        yield pre_state
    finally:
        remaining = db.query(models.Ortho).filter_by(project_id=project.id).count()
        transition_if(db, project, {_UPLOADING}, pre_state if remaining else "CREATED")


@router.patch("/projects/{project_id}")
@router.patch("/project")
def update_project(
    body: ProjectUpdate,
    project=Depends(get_project),
    db: Session = Depends(get_db),
):
    """Edit parameters and prepare a re-run on the SAME uploaded ortho.

    If the current run has already been used (analyzed/finalized/failed), its
    summary is archived to ``runs`` and ``current_run`` is bumped so the next
    analyze computes into a fresh ``work/run_<n+1>`` folder - previous runs are
    preserved on disk. If the current run hasn't been analyzed yet, params are
    just updated in place. Either way the project ends in ``UPLOADED``, ready for
    ``POST /runs/analyze``.
    """
    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": "Cannot change parameters while a run is in progress",
            "project_id": project.id,
        })
    if not project.orthos:
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "Upload an orthomosaic before configuring a re-run",
            "project_id": project.id,
        })

    # Type-check provided param overrides up front so a bad value fails here with
    # 400 instead of deep in the worker with 500 (v4 section 8.3).
    if body.params:
        _validate_param_overrides(body.params)
        # Cross-field rules must be checked against the merged result, or an
        # invalid pair (e.g. area_min > stored area_max) would be persisted here
        # and only surface later at analyze time.
        _validate_merged_params(project, body.params)

    # Merge param overrides onto the existing params, then validate model choices.
    new_params = dict(project.params or {})
    if body.params:
        new_params.update(body.params)
    try:
        if body.model_key is not None:
            resolve_model_path(body.model_key)
        if "model_name" in new_params:
            new_params["model_name"] = resolve_backbone(new_params.get("model_name"))
    except ValueError as e:
        raise HTTPException(400, {"code": "BAD_REQUEST", "message": str(e),
                                  "project_id": project.id})

    # Claim the state atomically before touching anything. The busy check above
    # is check-then-act: an analyze trigger can win the gap and start a run,
    # after which archiving the run and stamping UPLOADED here would bump
    # current_run out from under the running job and immediately re-open the
    # project for a second, concurrent analyze. Constraining the allowed source
    # to the exact state we validated against makes the loser fail cleanly.
    pre_state = project.state
    if not transition_if(db, project, {pre_state}, "UPLOADED"):
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": f"Project moved to {project.state} while being reconfigured",
            "project_id": project.id,
            "hint": "wait for the current run to finish, then retry",
        })

    if pre_state in USED_RUN_STATES:
        archive_current_run(db, project)

    project.params = new_params
    if body.model_key is not None:
        project.model_key = body.model_key
    if body.source_epsg is not None:
        project.source_epsg = body.source_epsg
    if body.run_name is not None:
        project.run_name = body.run_name.strip() or None
    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_dirs(project.id, project.current_run)
    return serialize_project(project)


@router.delete("/projects/{project_id}", status_code=204)
@router.delete("/project", status_code=204)
def delete_one(project=Depends(get_project), db: Session = Depends(get_db)):
    # Claim before deleting anything: without this, a delete landing while a job
    # runs pulls the whole project tree out from under it mid-compute. Any
    # non-busy state may be claimed; the transient DELETING state is never
    # observable, since the row goes away in the same request.
    if not transition_if(db, project, _DELETABLE_STATES, _DELETING):
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": f"Cannot delete while a run is in progress (state {project.state})",
            "project_id": project.id,
            "hint": "wait for the current run to finish",
        })
    delete_project_dir(project.id)
    db.delete(project)
    db.commit()
    return None


@router.post("/projects/{project_id}/orthomosaic")
@router.post("/project/orthomosaic")
def upload_ortho(
    project=Depends(get_project),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload the project's orthomosaic.

    Each project holds **exactly one** orthomosaic. If one is already present
    when this is called, the previous file is deleted from disk and its DB row
    is replaced, so the call has set-semantics rather than append-semantics.

    Locked (423 ORTHO_LOCKED) once the current run has been analyzed - the
    dataset can only change on a freshly-opened run.
    """
    _assert_ortho_unlocked(project)
    if not (file.filename or "").lower().endswith((".tif", ".tiff")):
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "Orthomosaic must be a .tif/.tiff GeoTIFF",
            "project_id": project.id,
        })

    with _dataset_edit_lock(db, project):
        paths = ensure_project_dirs(project.id, project.current_run)
        _clear_existing_orthos(project, db, paths)

        stem = os.path.splitext(os.path.basename(file.filename))[0]
        dst = os.path.join(paths["input_ortho"], f"{stem}.tif")
        _stream_to_disk(file, dst, max_bytes=settings.max_upload_mb * 1024 * 1024)
        return _register_ortho(project, db, dst, stem)


@router.post("/projects/{project_id}/orthomosaic/from-url")
@router.post("/project/orthomosaic/from-url")
def upload_ortho_from_url(
    body: OrthoFromUrl,
    project=Depends(get_project),
    db: Session = Depends(get_db),
):
    """Register an orthomosaic by downloading it from a public Google Drive link.

    Synchronous: the request blocks while the server downloads the file, so set a
    long client read timeout for large orthos. Only Google Drive share links set to
    'anyone with the link' are supported. Same set-semantics as the file upload -
    any previously-registered ortho is replaced. Locked (423 ORTHO_LOCKED) once
    the current run has been analyzed.
    """
    _assert_ortho_unlocked(project)
    url = (body.url or "").strip()
    host = urlparse(url).netloc.lower()
    if not (host == "drive.google.com" or host.endswith(".google.com")):
        raise HTTPException(400, {"code": "BAD_REQUEST",
            "message": "Only Google Drive links are supported.", "project_id": project.id})

    file_id = _extract_drive_id(url)
    if not file_id:
        raise HTTPException(400, {"code": "BAD_REQUEST",
            "message": "Could not parse a Google Drive file id from the URL.",
            "project_id": project.id})

    try:
        import gdown
    except ImportError:
        raise HTTPException(503, {"code": "DEPENDENCY_MISSING",
            "message": "Server is missing the 'gdown' dependency required for URL uploads."})

    with _dataset_edit_lock(db, project):
        return _download_ortho_from_drive(project, db, file_id, gdown)


def _download_ortho_from_drive(project, db: Session, file_id: str, gdown):
    """Body of the from-URL upload; runs under ``_dataset_edit_lock``."""
    paths = ensure_project_dirs(project.id, project.current_run)
    tmp_dir = tempfile.mkdtemp(prefix="ortho_dl_", dir=paths["input_ortho"])
    try:
        try:
            out = gdown.download(id=file_id, output=tmp_dir + os.sep, quiet=True)
        except Exception as e:
            raise HTTPException(400, {"code": "BAD_REQUEST",
                "message": f"Google Drive download failed: {e}", "project_id": project.id})
        if not out or not os.path.exists(out):
            raise HTTPException(400, {"code": "BAD_REQUEST",
                "message": ("Download failed - the file may be private, deleted, or over "
                            "its Google Drive download quota."), "project_id": project.id})

        max_bytes = settings.max_upload_mb * 1024 * 1024
        if os.path.getsize(out) > max_bytes:
            raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE",
                "message": f"Downloaded file exceeds the {settings.max_upload_mb} MB limit.",
                "project_id": project.id})
        if not out.lower().endswith((".tif", ".tiff")):
            raise HTTPException(400, {"code": "BAD_REQUEST",
                "message": "The Drive file is not a .tif/.tiff GeoTIFF.", "project_id": project.id})

        stem = os.path.splitext(os.path.basename(out))[0]
        dst = os.path.join(paths["input_ortho"], f"{stem}.tif")
        _clear_existing_orthos(project, db, paths)
        shutil.move(out, dst)
        return _register_ortho(project, db, dst, stem)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# -- ground-truth zip safety limits (defend against bombs / hostile archives) --
_GT_MAX_MEMBERS = 10000                          # absurd file counts -> reject
_GT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024     # 2 GiB uncompressed budget
_GT_MAX_FILE_BYTES = 250 * 1024 * 1024           # 250 MiB per extracted file
_GT_MAX_RATIO = 200                              # uncompressed/compressed ratio guard


@router.post("/projects/{project_id}/ground-truth")
@router.post("/project/ground-truth")
def upload_ground_truth(
    project=Depends(get_project),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a .zip whose top-level folders are species names containing crown
    .tif files (the structure step3_validate expects).

    Hardened against zip bombs and hostile archives: the compressed upload is
    size-capped, the archive is inspected *before* anything is written, and only
    regular ``*.tif`` members are extracted - each into ``<species>/<file>.tif``
    with a sanitized path and copied through per-file and total-size budgets, so
    a lying header or a decompression bomb cannot fill the disk. Existing ground
    truth is replaced (set-semantics).
    """
    _assert_ortho_unlocked(project)   # freeze dataset (ortho + GT) after first analysis
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, {"code": "BAD_ARCHIVE",
            "message": "Ground truth must be a .zip of <species>/*.tif folders",
            "project_id": project.id})

    import zipfile

    with _dataset_edit_lock(db, project):
        paths = ensure_project_dirs(project.id, project.current_run)
        # Stage the upload OUTSIDE input_gt so input_gt can be wiped for
        # set-semantics. The filename is fixed, so two concurrent uploads would
        # overwrite each other's staging file — the lock is what prevents it.
        tmp = os.path.join(paths["root"], "_gt_upload.zip")
        _stream_to_disk(file, tmp, max_bytes=settings.max_upload_mb * 1024 * 1024)

        try:
            try:
                zf = zipfile.ZipFile(tmp)
            except zipfile.BadZipFile:
                raise HTTPException(400, {"code": "BAD_ARCHIVE",
                    "message": "File is not a valid .zip archive", "project_id": project.id})
            with zf as z:
                extracted = _safe_extract_gt_tifs(z, paths["input_gt"])
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    if extracted == 0:
        raise HTTPException(400, {"code": "BAD_ARCHIVE",
            "message": "Archive contained no usable .tif ground-truth images",
            "project_id": project.id})

    species = sorted(
        d for d in os.listdir(paths["input_gt"])
        if os.path.isdir(os.path.join(paths["input_gt"], d))
    )
    return {
        "project_id": project.id,
        "state": project.state,
        "species_folders": species,
        "files_extracted": extracted,
    }


# -- helpers --------------------------------------------------------------
_DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
]


def _extract_drive_id(url: str) -> str | None:
    """Pull the file id out of common Google Drive URL shapes."""
    for rx in _DRIVE_ID_PATTERNS:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def _clear_existing_orthos(project, db: Session, paths: dict) -> None:
    """Delete any previously-registered ortho (file + DB row) - set-semantics."""
    for existing in db.query(models.Ortho).filter_by(project_id=project.id).all():
        prev_path = os.path.join(paths["input_ortho"], existing.filename)
        if os.path.exists(prev_path):
            try:
                os.remove(prev_path)
            except OSError:
                pass
        db.delete(existing)


def _register_ortho(project, db: Session, dst: str, stem: str):
    """Record raster metadata, create the Ortho row, advance state, and serialize."""
    meta = _raster_meta(dst)
    if meta.get("crs_epsg") and not project.source_epsg:
        project.source_epsg = meta["crs_epsg"]

    o = models.Ortho(project_id=project.id, stem=stem)
    o.filename = f"{stem}.tif"
    o.width = meta.get("width")
    o.height = meta.get("height")
    o.crs = meta.get("crs")
    o.bands = meta.get("bands")
    o.size_bytes = os.path.getsize(dst)
    db.add(o)
    db.add(project)
    db.commit()

    # Runs under _dataset_edit_lock, so the project is in the transient
    # UPLOADING state and this release is what publishes the result. Conditional
    # so it can never overwrite a state someone else legitimately set.
    transition_if(db, project, {_UPLOADING, "CREATED", "UPLOADED"}, "UPLOADED")
    db.refresh(project)
    return serialize_project(project)


def _validate_param_overrides(overrides: dict) -> None:
    """Validate provided pipeline-param overrides against their declared types
    AND their declared bounds, so a bad value fails fast with 400 (v4 section
    8.3). Unknown keys are left untouched.

    ``fields[key].annotation`` is the bare type — pydantic keeps ge/le/gt/lt on
    the FieldInfo, not the annotation, so validating the annotation alone
    silently ignores every bound. Re-attaching the FieldInfo via Annotated is
    what makes the constraints in PipelineParams actually enforce.

    This checks one key at a time and therefore cannot see cross-field rules
    such as area_min < area_max; those live in PipelineParams' model_validator
    and are applied to the merged params by _validate_trigger_body (runs.py).
    """
    from typing import Annotated

    from pydantic import TypeAdapter

    from app.schemas.project import PipelineParams
    fields = PipelineParams.model_fields
    for key, val in (overrides or {}).items():
        if key in fields:
            try:
                TypeAdapter(
                    Annotated[fields[key].annotation, fields[key]]
                ).validate_python(val)
            except Exception as e:
                raise HTTPException(400, {"code": "BAD_REQUEST",
                    "message": f"Invalid value for param '{key}': {e}"})


def _validate_merged_params(project, overrides: dict) -> None:
    """Apply PipelineParams' cross-field rules to the params as they will be
    STORED, not just to the incoming overrides.

    Both the project-update path and the analyze trigger merge overrides onto
    the project's existing params, so per-key validation is not enough: sending
    only ``area_min: 5000`` passes every individual bound while still producing
    an invalid pair against a stored ``area_max`` of 2000. Called from both
    write paths so a bad combination can never be persisted.

    Unknown keys are dropped before validating, so legacy or operator-only
    params already on the project cannot break the check.
    """
    from pydantic import ValidationError

    from app.schemas.project import PipelineParams

    merged = {**(getattr(project, "params", None) or {}), **(overrides or {})}
    known = {k: v for k, v in merged.items() if k in PipelineParams.model_fields}
    try:
        PipelineParams(**known)
    except ValidationError as e:
        first = e.errors()[0]
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": first.get("msg", str(e)),
            "project_id": project.id,
        })


def _stream_to_disk(
    file: UploadFile, dst: str, chunk: int = 1024 * 1024, max_bytes: int | None = None
) -> None:
    """Stream an upload to disk. If ``max_bytes`` is given, abort (and delete the
    partial file) as soon as the body exceeds it - so an oversized upload can
    never be fully written."""
    written = 0
    with open(dst, "wb") as out:
        while True:
            data = file.file.read(chunk)
            if not data:
                break
            written += len(data)
            if max_bytes is not None and written > max_bytes:
                out.close()
                try:
                    os.remove(dst)
                except OSError:
                    pass
                file.file.close()
                raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE",
                    "message": f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit."})
            out.write(data)
    file.file.close()


def _raster_meta(path: str) -> dict:
    """Best-effort raster metadata; empty dict if rasterio is unavailable."""
    try:
        import rasterio

        with rasterio.open(path) as src:
            try:
                epsg = src.crs.to_epsg() if src.crs else None
            except Exception:
                epsg = None
            return {
                "width": src.width,
                "height": src.height,
                "crs": str(src.crs) if src.crs else None,
                "crs_epsg": epsg,
                "bands": src.count,
            }
    except Exception:
        return {}


def _safe_member_path(name: str) -> str | None:
    """Return a sanitized ``<species>/<file>`` path for a zip member, or None if
    unsafe. Strips drive letters and leading separators, rejects ``..`` and
    absolute paths, flattens deep nesting to ``<species>/<file>``, and limits
    each component to a safe charset."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    fname = parts[-1]
    species = parts[-2] if len(parts) >= 2 else "unlabelled"

    def _clean(seg: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", seg).strip("._") or "x"

    safe_name = _clean(fname)
    if not safe_name.lower().endswith(".tif"):
        return None
    return os.path.join(_clean(species), safe_name)


def _safe_extract_gt_tifs(z, dest: str) -> int:
    """Validate a ground-truth zip and extract only safe ``*.tif`` members.

    Defends against zip bombs (member-count, total-size and per-member
    compression-ratio caps) and hostile paths (traversal, absolute paths,
    symlinks/devices). Returns the number of files written.
    """
    infos = z.infolist()
    if len(infos) > _GT_MAX_MEMBERS:
        raise HTTPException(400, {"code": "BAD_ARCHIVE", "message": f"Archive has too many entries (> {_GT_MAX_MEMBERS})"})

    # Pre-flight on the headers: catch obvious bombs before writing a single byte.
    declared_total = 0
    for info in infos:
        if info.is_dir():
            continue
        declared_total += info.file_size
        if info.file_size > _GT_MAX_FILE_BYTES:
            raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "message": f"Archive contains an oversized member: {info.filename}"})
        if info.compress_size > 0 and (info.file_size / info.compress_size) > _GT_MAX_RATIO:
            raise HTTPException(400, {"code": "BAD_ARCHIVE", "message": "Archive looks like a decompression bomb (suspicious ratio)"})
    if declared_total > _GT_MAX_TOTAL_BYTES:
        raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "message": "Archive exceeds the uncompressed size budget"})

    # Set-semantics: drop any previous ground truth, then recreate the folder.
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    written_total = 0
    count = 0
    for info in infos:
        if info.is_dir():
            continue
        # Skip anything that isn't a regular file (symlink/device/fifo). A mode of
        # 0 (common for Windows-made zips) is treated as a regular file.
        mode = (info.external_attr >> 16) & 0o170000
        if mode and mode != 0o100000:
            continue
        if not info.filename.lower().endswith(".tif"):
            continue
        rel = _safe_member_path(info.filename)
        if rel is None:
            continue
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
        # Stream-copy enforcing REAL byte counts (never trust the header).
        file_bytes = 0
        with z.open(info) as src, open(target, "wb") as out:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                file_bytes += len(buf)
                written_total += len(buf)
                if file_bytes > _GT_MAX_FILE_BYTES or written_total > _GT_MAX_TOTAL_BYTES:
                    out.close()
                    try:
                        os.remove(target)
                    except OSError:
                        pass
                    raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "message": "Archive exceeds size limits during extraction"})
                out.write(buf)
        count += 1
    return count
