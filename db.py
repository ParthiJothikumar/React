"""Database class (connection factory) and row adapters.

Database.connect() opens a per-request DB-API 2.0 connection to either a local SQLite
file (testing) or Azure SQL (production); both use '?' placeholders so every query in
repositories.py works unchanged on either.

Why a class: the SQLite "have I created the tables yet?" flag used to be a module
global, which meant it could not be reset between tests and was shared by every code
path in the process. As instance state on one object it has a clear owner and a
lifecycle, and the object can be swapped for a stub.

The two row adapters stay module-level functions on purpose: they hold no state, they
just zip a tuple against cur.description. Wrapping them in a class would add a `self`
that never gets used.
"""
import sqlite3
from typing import Optional

from app.config import Settings, logger
from app.errors import DatabaseUnavailable

# Schema for SQLite mode. On a Function App nobody runs setup_sqlite.py, so we
# create the tables on first connect (idempotent) to avoid "no such table".
# The column set MUST mirror schema.sql (Azure SQL) so test and prod don't drift.
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT NOT NULL, user_id TEXT NOT NULL, conversation_id TEXT, stage TEXT,
    vars TEXT, question TEXT, answer TEXT, session_id TEXT, seq INTEGER,
    title TEXT, created_at TEXT, updated_at TEXT,
    job_id TEXT, job_status TEXT, job_message TEXT, job_output TEXT,
    job_baseline TEXT,
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS IX_conversations_user_session
    ON conversations (user_id, session_id);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT NOT NULL, session_id TEXT, user_id TEXT NOT NULL,
    current_conversation_id TEXT, title TEXT, created_at TEXT, updated_at TEXT,
    PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT,
    PRIMARY KEY (user_id, conversation_id, seq)
);
"""


class Database:
    """Opens connections to the configured state store (SQLite or Azure SQL)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._sqlite_ready = False
        self._configure_pool()

    @property
    def use_sqlite(self) -> bool:
        return self._settings.use_sqlite

    def _configure_pool(self) -> None:
        """Set the driver's connection-pool limits once, at startup.

        This MUST run before the first connection: mssql-python ignores pooling
        settings once any connection exists. deps.py builds this object when it is
        imported, which is well before the first request.

        Deliberately best-effort. A missing or older driver logs a warning rather
        than raising, because raising here would abort app startup and take down
        every endpoint including the health check. If the driver genuinely isn't
        available, connect() raises DatabaseUnavailable per request -- which is the
        right place for that failure to surface.
        """
        if self._settings.use_sqlite:
            return  # SQLite mode: the SQL driver isn't needed or installed
        try:
            import mssql_python

            mssql_python.pooling(
                max_size=self._settings.SQL_POOL_MAX_SIZE,
                idle_timeout=self._settings.SQL_POOL_IDLE_TIMEOUT,
            )
            #max_size=45 doesn't open 45 connections at startup. It opens them on demand and closes idle ones after idle_timeout
            logger.info(
                "SQL connection pool configured: max_size=%s idle_timeout=%ss",
                self._settings.SQL_POOL_MAX_SIZE,
                self._settings.SQL_POOL_IDLE_TIMEOUT,
            )
        except Exception:
            logger.exception(
                "could not configure the SQL connection pool; the driver's own "
                "defaults (max_size=100, idle_timeout=600) remain in effect"
            )

    def connect(self):
        """Open a DB connection (local SQLite, or Azure SQL).

        When SQLITE_DB_PATH is set we open a local SQLite file (built into Python --
        no install/network/driver needed), ideal for offline testing. Otherwise we
        open Azure SQL via mssql_python + SQL_CONNECTION_STRING. Both are DB-API 2.0
        with '?' placeholders, so every query in repositories.py works on either.

        A new connection per request keeps things thread-safe (FastAPI runs sync
        endpoints on a threadpool).

        NOTE: SQLite is for TESTING only. On a Function App the file lives on the
        per-instance temp disk, so it is wiped on restart/scale and not shared
        across instances. Use Azure SQL for anything that must persist.
        """
        if self.use_sqlite:
            conn = sqlite3.connect(self._settings.SQLITE_DB_PATH)
            if not self._sqlite_ready:
                conn.executescript(SQLITE_SCHEMA)
                conn.commit()
                self._sqlite_ready = True
            return conn

        conn_str = self._settings.SQL_CONNECTION_STRING
        if not conn_str:
            logger.error("SQL_CONNECTION_STRING not configured")
            raise DatabaseUnavailable("SQL_CONNECTION_STRING not configured")

        # lazy import: only needed for Azure SQL, so SQLite mode runs without the
        # driver installed. `mssql-python` is in requirements.txt for deployment.
        try:
            import mssql_python
        except ImportError:
            logger.exception("mssql_python driver not installed")
            raise DatabaseUnavailable("mssql_python driver not installed")

        try:
            return mssql_python.connect(conn_str)
        except Exception:
            logger.exception("Azure SQL connection failed")
            raise DatabaseUnavailable("Azure SQL connection failed")


def fetchone_dict(cur) -> Optional[dict]:
    """Return the row as a {column_name: value} dict, or None if no row.

    DB-API cursors hand back rows as plain tuples; we zip them with the column
    names (from cur.description) so callers can use row["stage"] instead of
    fragile positional indexing like row[3].
    """
    row = cur.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def fetchall_dicts(cur) -> list:
    """Return all rows as a list of {column_name: value} dicts (see fetchone_dict).

    Empty list when the query matched nothing.
    """
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
