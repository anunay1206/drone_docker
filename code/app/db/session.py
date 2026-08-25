from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.settings import settings

_IS_SQLITE = settings.database_url.startswith("sqlite")

# ``timeout`` is the SQLite busy timeout: how long a writer waits for another
# writer's lock before raising "database is locked". The default (5 s) is short
# for this workload — a worker thread commits job progress throughout a run
# while API requests write concurrently.
_connect_args = {"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """WAL so readers never block the writer (and vice versa).

        Under the default rollback journal, a long-running job's writes lock the
        whole file and concurrent status polls fail with "database is locked".
        WAL is persistent (set once per file) but re-asserting per connection is
        harmless and covers a freshly-created DB.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()


def get_db():
    """FastAPI dependency: yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables. For production use Alembic migrations instead."""
    from app.db import models  # noqa: F401  (register mappers)
    from app.db.base import Base

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_add_columns()
    _migrate_sqlite_unique_job_key()


def _migrate_sqlite_add_columns() -> None:
    """Dev-convenience migration: add columns create_all won't add to an existing
    SQLite file. Keeps older treecrown.db files working without a wipe.
    Use Alembic for real migrations / non-SQLite backends.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    wanted = {
        "projects": {
            "current_run": "INTEGER DEFAULT 1",
            "runs": "JSON",
            "run_name": "TEXT",
            "consent": "INTEGER DEFAULT 0",
            "consent_at": "DATETIME",
            "pruned_at": "DATETIME",
        },
        "jobs": {
            "request_id": "TEXT",
        },
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_sqlite_unique_job_key() -> None:
    """Add the jobs (project_id, celery_task_id) unique index to an existing DB.

    ``create_all`` only creates missing *tables*, so a table that predates the
    constraint never gets it. Historic double-runs may already hold duplicate
    pairs, which would make CREATE UNIQUE INDEX fail — those losers get their
    key cleared first (the row itself is kept: it is run history). The keeper is
    the successful attempt, else the most recent.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        idx = conn.exec_driver_sql("PRAGMA index_list(jobs)").fetchall()
        if any(row[1] == "uq_jobs_project_task" for row in idx):
            return
        conn.exec_driver_sql(
            """
            UPDATE jobs SET celery_task_id = NULL
             WHERE celery_task_id IS NOT NULL
               AND rowid NOT IN (
                   SELECT rowid FROM (
                       SELECT rowid, ROW_NUMBER() OVER (
                                  PARTITION BY project_id, celery_task_id
                                  ORDER BY (state = 'SUCCEEDED') DESC,
                                           started_at DESC, rowid DESC
                              ) AS rn
                         FROM jobs
                        WHERE celery_task_id IS NOT NULL
                   ) WHERE rn = 1
               )
            """
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_jobs_project_task "
            "ON jobs (project_id, celery_task_id)"
        )
