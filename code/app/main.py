"""FastAPI application entry point.

Run:
    uvicorn app.main:app --reload
"""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, get_logger, new_request_id, with_context
from app.db.session import init_db
from app.services import activity_log

log = get_logger("app.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()  # side-channel logging — set up before anything else
    init_db()          # dev convenience; use Alembic migrations in production
    yield


app = FastAPI(
    title="Tree-Crown Species Pipeline API",
    version="0.1.0",
    description=(
        "Two-job, stage-gated async service wrapping the tree-crown detection / "
        "clustering / species-mapping pipeline. "
        "See API_SPECIFICATION.docx and API_DESIGN.md."
    ),
    lifespan=lifespan,
)

# Normalise every error to the structured {"error": {...}} envelope (v4 §8).
install_error_handlers(app)

# CORS — open during development. `allow_origins=["*"]` cannot be combined with
# `allow_credentials=True`; this API authenticates via the X-API-Key header (not
# cookies), so credentials are not needed. Tighten allow_origins to your real
# frontend origin(s) before deploying.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Per-user audit middleware ──────────────────────────────────────────
# Records who called mutating endpoints, using the Google-sign-in headers set
# by the frontend (X-User-Email / X-User-Id). Reads/health/CORS-preflight are
# skipped to keep the ledger signal-heavy. Best-effort; never blocks a request.
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Airflow-facing compute callbacks — never touch these responses (no X-Request-Id
# header). Byte-identical bodies/status per the Airflow contract. Covers the
# compute callbacks (/api/v1/compute/*) and the DAG->backend callback variants
# (drone_api / drone_status / analyze / finalize) under /api/v1/project(s).
_COMPUTE_PATH_PREFIXES = (
    "/api/v1/compute",
    "/api/v1/project/drone_api",
    "/api/v1/project/drone_status",
    "/api/v1/project/analyze",
    "/api/v1/project/finalize",
)


def _is_compute_path(path: str) -> bool:
    if path.startswith(_COMPUTE_PATH_PREFIXES):
        return True
    # /api/v1/projects/{id}/analyze and /finalize are compute callbacks too.
    return path.startswith("/api/v1/projects/") and (
        path.endswith("/analyze") or path.endswith("/finalize")
    )


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    # Mint (or reuse) a correlation id and bind it for the request scope.
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    client_ip = request.headers.get("X-Forwarded-For")
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host

    started = time.monotonic()
    with with_context(request_id=request_id):
        response = await call_next(request)
    latency_ms = int((time.monotonic() - started) * 1000)

    path = request.url.path
    # Side-channel header ONLY on non-compute /api/ paths (Airflow untouched).
    if path.startswith("/api/") and not _is_compute_path(path):
        response.headers["X-Request-Id"] = request_id

    try:
        with with_context(request_id=request_id):
            if response.status_code >= 500:
                log.error(
                    "%s %s -> %s (%dms)",
                    request.method, path, response.status_code, latency_ms,
                )
            else:
                log.info(
                    "%s %s -> %s (%dms)",
                    request.method, path, response.status_code, latency_ms,
                )
    except Exception:
        pass

    try:
        if request.method in _AUDIT_METHODS and path.startswith("/api/"):
            activity_log.append(
                email=request.headers.get("X-User-Email"),
                user_id=request.headers.get("X-User-Id"),
                action=f"{request.method} {path}",
                method=request.method,
                path=path,
                project_id=request.path_params.get("project_id")
                if hasattr(request, "path_params") else None,
                status=response.status_code,
                request_id=request_id,
                client_ip=client_ip,
            )
    except Exception:
        pass
    return response


@app.get("/livez", tags=["meta"])
def livez():
    """Liveness probe - is the process up. Used by Docker/K8s healthchecks."""
    return {"status": "ok"}


app.include_router(api_router)
