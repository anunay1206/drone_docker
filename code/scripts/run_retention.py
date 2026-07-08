#!/usr/bin/env python3
"""Consent-aware retention cleanup — standalone, cron-driven (no Celery/Redis).

Deletes / prunes projects whose last activity (``updated_at``) is older than
``settings.retention_days``, branching on each project's data-sharing consent:

    consent 0 (No)             -> delete the whole storage folder + DB row
    consent 1 (Yes, all)       -> retained (skip) when settings.retain_consent_all
    consent 2 (unlabelled only)-> keep everything through Step 1; delete the
                                  labelled outputs (step2/3/4) + ClusterLabel rows,
                                  set state=PRUNED, stamp pruned_at

Safety (single workstation, may be interrupted mid-run):
  * a non-blocking file lock (fcntl.flock) → only one run at a time; the lock is
    released automatically when the process exits, even on crash/kill.
  * commit after every project → interruption leaves finished ones final; the
    rest are handled on the next cron run.
  * all filesystem ops are idempotent (rmtree ignore_errors); a consent=2 project
    already carrying pruned_at is skipped, so a half-done sweep just resumes.
  * projects that are ANALYZING / FINALIZING are never touched.

Usage (from the ``code/`` dir, same env as the API — DB + storage volume):
    python scripts/run_retention.py            # act
    python scripts/run_retention.py --dry-run  # log only, change nothing

Cron (daily 03:00):
    0 3 * * *  cd /code && python scripts/run_retention.py >> /data/storage/retention.log 2>&1

See docs/RETENTION_CONSENT_CLEANUP_PLAN.md.
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# Make the app package importable when run as `python scripts/run_retention.py`
# from the code/ directory (scripts/ is a sibling of app/).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.logging import (                               # noqa: E402
    configure_logging,
    naive_from_ts,
    naive_now,
    now_ist,
)
from app.core.settings import settings                       # noqa: E402
from app.core.storage import (                               # noqa: E402
    delete_project_dir,
    prune_labelled_outputs,
    project_root,
)
from app.db import models                                    # noqa: E402
from app.db.session import SessionLocal                      # noqa: E402

_BUSY_STATES = {"ANALYZING", "FINALIZING"}

log = logging.getLogger("app.retention")


def _log(msg: str) -> None:
    # Keep the print (cron redirect to retention.log) AND emit via the logger so
    # the central app.log / errors.jsonl capture it too (plan §2 / §8).
    print(f"[retention {now_ist():%Y-%m-%d %H:%M:%S IST}] {msg}", flush=True)
    log.info(msg)


def _acquire_lock():
    """Non-blocking file lock. Returns the open file handle (keep it alive for the
    duration) or None if another run holds it. Auto-released on process exit."""
    lock_path = os.path.join(settings.storage_root, ".retention.lock")
    try:
        os.makedirs(settings.storage_root, exist_ok=True)
        import fcntl

        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        fh.write(f"{os.getpid()} {datetime.utcnow().isoformat()}\n")
        fh.flush()
        return fh
    except Exception:
        # No fcntl (e.g. Windows) / other error → proceed lock-free. Fine for a
        # single scheduled runner.
        return open(lock_path, "a") if os.path.isdir(settings.storage_root) else None


def _runs_of(project) -> range:
    return range(1, (getattr(project, "current_run", 1) or 1) + 1)


def run(dry_run: bool = False) -> dict:
    cutoff = naive_now() - timedelta(days=settings.retention_days)
    summary = {"deleted": [], "pruned": [], "skipped_busy": [],
               "retained": [], "orphans": [], "dry_run": dry_run}
    db = SessionLocal()
    try:
        expired = (
            db.query(models.Project)
            .filter(models.Project.updated_at < cutoff)
            .all()
        )
        for p in expired:
            if p.state in _BUSY_STATES:
                summary["skipped_busy"].append(p.id)
                continue
            consent = getattr(p, "consent", 0) or 0

            if consent == 1 and settings.retain_consent_all:
                summary["retained"].append(p.id)
                continue

            if consent == 2:
                if getattr(p, "pruned_at", None):
                    continue                                  # already pruned
                summary["pruned"].append(p.id)
                if dry_run:
                    continue
                try:
                    for r in _runs_of(p):
                        prune_labelled_outputs(p.id, r)
                    db.query(models.ClusterLabel).filter_by(project_id=p.id).delete()
                    p.state = "PRUNED"
                    p.pruned_at = naive_now()
                    db.add(p)
                    db.commit()                               # per-project commit
                except Exception:
                    log.error("prune failed for project %s", p.id, exc_info=True)
                    db.rollback()
                continue

            # consent 0 (No) — or 1 with retain disabled: full delete
            summary["deleted"].append(p.id)
            if dry_run:
                continue
            try:
                delete_project_dir(p.id)
                db.delete(p)
                db.commit()
            except Exception:
                log.error("delete failed for project %s", p.id, exc_info=True)
                db.rollback()

        # Orphan sweep: folders on disk with no DB row, older than the cutoff.
        live_ids = {pid for (pid,) in db.query(models.Project.id).all()}
        projects_dir = os.path.join(settings.storage_root, "projects")
        if os.path.isdir(projects_dir):
            for name in os.listdir(projects_dir):
                full = os.path.join(projects_dir, name)
                if name in live_ids or not os.path.isdir(full):
                    continue
                try:
                    mtime = naive_from_ts(os.path.getmtime(full))
                except OSError:
                    continue
                if mtime < cutoff:
                    summary["orphans"].append(name)
                    if not dry_run:
                        import shutil
                        shutil.rmtree(full, ignore_errors=True)
    finally:
        db.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Consent-aware retention cleanup.")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would happen; change nothing")
    args = ap.parse_args()

    configure_logging()

    lock = _acquire_lock()
    if lock is None:
        _log("another retention run holds the lock — exiting.")
        return 0
    try:
        _log(f"start (retention_days={settings.retention_days}, dry_run={args.dry_run})")
        s = run(dry_run=args.dry_run)
        _log("deleted={d} pruned={p} retained={r} skipped_busy={b} orphans={o}".format(
            d=len(s["deleted"]), p=len(s["pruned"]), r=len(s["retained"]),
            b=len(s["skipped_busy"]), o=len(s["orphans"])))
        for k in ("deleted", "pruned", "orphans"):
            if s[k]:
                _log(f"{k}: {', '.join(s[k])}")
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
