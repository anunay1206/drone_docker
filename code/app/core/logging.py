"""Central logging (side-channel only) — see docs/ERROR_LOGGING_PLAN.md §1.

Nothing in this module ever mutates an HTTP response, adds a header to a
compute response, or changes a status code. It provides:

  * ContextVars for the correlation ids (request_id, job_id, project_id,
    dag_run_id, stage) plus a ``ContextFilter`` that injects them onto every
    ``LogRecord`` (missing -> ``"-"``).
  * A ``JsonFormatter`` (one JSON object per line, exception included) and a
    text formatter for readable dev logs.
  * ``configure_logging(force=False)`` — idempotent root-logger setup:
    stdout StreamHandler + rotating ``app.log`` (all levels) + a dedicated
    rotating ``errors.jsonl`` (ERROR-only, always JSON).
  * Helpers: ``get_logger``, ``with_context``, ``new_request_id`` and
    ``classify_conn_error`` (imported later by services + endpoints).
  * ``ERROR_CODES`` — canonical §10 error-code catalog other modules import.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import socket
import uuid

from app.core.settings import settings

# ── correlation-id context ─────────────────────────────────────────────
# ContextVars default to "-" so a LogRecord always has a value even outside a
# request/task scope. (Note: ContextVars do NOT inherit into a plain daemon
# thread — task bodies re-bind these from the Job row; see the plan §2.)
_DEFAULT = "-"
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=_DEFAULT
)
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "job_id", default=_DEFAULT
)
project_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "project_id", default=_DEFAULT
)
dag_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "dag_run_id", default=_DEFAULT
)
stage_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stage", default=_DEFAULT
)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "job_id": job_id_var,
    "project_id": project_id_var,
    "dag_run_id": dag_run_id_var,
    "stage": stage_var,
}


class ContextFilter(logging.Filter):
    """Inject the correlation ContextVars onto every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXT_VARS.items():
            if not hasattr(record, name):
                try:
                    setattr(record, name, var.get())
                except LookupError:
                    setattr(record, name, _DEFAULT)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including the exception if present."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in _CONTEXT_VARS:
            payload[name] = getattr(record, name, _DEFAULT)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # Include any extra structured fields attached via logger(..., extra=...)
        for key, value in record.__dict__.items():
            if key in payload or key in _RESERVED_RECORD_KEYS:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


# LogRecord attributes we never want to duplicate into the JSON "extra" scan.
_RESERVED_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
    *(_CONTEXT_VARS.keys()),
}

_TEXT_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)s "
    "[req=%(request_id)s job=%(job_id)s proj=%(project_id)s "
    "dag=%(dag_run_id)s stage=%(stage)s] %(message)s"
)

_CONFIGURED = False


def configure_logging(force: bool = False) -> None:
    """Idempotent root-logger setup. Safe to call multiple times.

    ``force=True`` tears down existing handlers first (needed after a Celery
    prefork fork, which drops handlers set up in the parent process).
    """
    global _CONFIGURED
    root = logging.getLogger()

    if _CONFIGURED and not force:
        return

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()

    # Guard against duplicate handlers if called twice without force.
    if root.handlers and not force:
        _CONFIGURED = True
        return

    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    root.setLevel(level)

    context_filter = ContextFilter()
    text_formatter = logging.Formatter(_TEXT_FORMAT)
    json_formatter = JsonFormatter()
    line_formatter = json_formatter if settings.log_json else text_formatter

    # stdout — for container log collection.
    import sys

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(line_formatter)
    stream.addFilter(context_filter)
    root.addHandler(stream)

    # Rotating app.log (all levels) + dedicated errors.jsonl (ERROR-only, JSON).
    try:
        os.makedirs(settings.log_dir, exist_ok=True)

        app_log = logging.handlers.RotatingFileHandler(
            os.path.join(settings.log_dir, "app.log"),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        app_log.setFormatter(line_formatter)
        app_log.addFilter(context_filter)
        root.addHandler(app_log)

        errors_log = logging.handlers.RotatingFileHandler(
            os.path.join(settings.log_dir, "errors.jsonl"),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        errors_log.setLevel(logging.ERROR)
        errors_log.setFormatter(json_formatter)  # errors.jsonl is ALWAYS JSON
        errors_log.addFilter(context_filter)
        root.addHandler(errors_log)
    except OSError:
        # Filesystem not writable (e.g. read-only local dev): keep stdout only.
        logging.getLogger("app.logging").warning(
            "could not open log_dir %s; file logging disabled", settings.log_dir,
            exc_info=True,
        )

    logging.captureWarnings(True)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger (e.g. ``get_logger("app.api.runs")``)."""
    return logging.getLogger(name)


@contextlib.contextmanager
def with_context(**kw):
    """Set the correlation ContextVars for the duration of the ``with`` block.

    Only the provided keys are touched; each is reset to its prior value on
    exit. Unknown keys are ignored.
    """
    tokens = []
    for key, value in kw.items():
        var = _CONTEXT_VARS.get(key)
        if var is None:
            continue
        tokens.append((var, var.set(_DEFAULT if value is None else str(value))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def new_request_id() -> str:
    """A fresh correlation id (uuid4 hex)."""
    return uuid.uuid4().hex


def classify_conn_error(exc: Exception, timeout=None) -> str:
    """Map a connection/outbound exception to a human-readable reason string.

    Used by services + endpoints to describe a *failed outbound call* (never
    the payload sent to Airflow). See plan §10.
    """
    if exc is None:
        return "no response received"

    reason = getattr(exc, "reason", None)
    target = reason if reason is not None else exc

    if isinstance(target, ConnectionRefusedError):
        return "connection refused"
    if isinstance(target, (socket.timeout, TimeoutError)):
        return "timed out" + (f" after {timeout}s" if timeout is not None else "")
    if isinstance(target, socket.gaierror):
        return "host not found (DNS)"

    if reason is not None:
        text = str(reason).strip()
        return text or "no response received"

    text = str(exc).strip()
    return text or "no response received"


# ── canonical error-code catalog (plan §10) ────────────────────────────
# Other modules import these constants so codes stay consistent across the
# raising sites, the response envelope, and the log lines.
ERROR_CODES = {
    "MISSING_PARAM": "MISSING_PARAM",
    "INVALID_PARAM": "INVALID_PARAM",
    "NO_ORTHO": "NO_ORTHO",
    "NO_LABELS": "NO_LABELS",
    "INVALID_STATE": "INVALID_STATE",
    "CONFLICT_BUSY": "CONFLICT_BUSY",
    "UPLOAD_TOO_LARGE": "UPLOAD_TOO_LARGE",
    "BAD_FORMAT": "BAD_FORMAT",
    "DRIVE_FETCH_FAILED": "DRIVE_FETCH_FAILED",
    "AIRFLOW_UNREACHABLE": "AIRFLOW_UNREACHABLE",
    "AIRFLOW_HTTP_ERROR": "AIRFLOW_HTTP_ERROR",
    "FILEBROWSER_UNREACHABLE": "FILEBROWSER_UNREACHABLE",
    "FILEBROWSER_AUTH": "FILEBROWSER_AUTH",
    "FILEBROWSER_SHARE_FAILED": "FILEBROWSER_SHARE_FAILED",
    "COMPUTE_FAILED": "COMPUTE_FAILED",
    "STORAGE_ERROR": "STORAGE_ERROR",
    "UNAUTHENTICATED": "UNAUTHENTICATED",
}
