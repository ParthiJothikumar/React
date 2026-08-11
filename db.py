"""Azure SQL / SQLite connection helpers and row adapters.

get_conn() opens a per-request DB-API 2.0 connection to either a local SQLite file
(testing) or Azure SQL (production); both use '?' placeholders so every query works
unchanged on either. _fetchone_dict/_fetchall_dicts turn tuple rows into
{column: value} dicts so callers use row["stage"] instead of positional indexing.
"""
import os
import sqlite3
from typing import Optional

from fastapi import HTTPException

from app.config import SQLITE_DB_PATH, logger

# Schema for SQLite mode. On a Function App nobody runs setup_sqlite.py, so we
# create the tables on first connect (idempotent) to avoid "no such table".
# The column set MUST mirror schema.sql (Azure SQL) so test and prod don't drift.
# previous_conversation_id/summary/rolled_over are legacy rollover columns kept
# only for that parity; the current code neither reads nor writes them.
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT NOT NULL, user_id TEXT NOT NULL, conversation_id TEXT, stage TEXT,
    vars TEXT, question TEXT, answer TEXT, session_id TEXT, seq INTEGER,
    previous_conversation_id TEXT, summary TEXT, title TEXT, rolled_over INTEGER,
    created_at TEXT, updated_at TEXT,
    job_id TEXT, job_status TEXT, job_progress TEXT, job_result TEXT, job_baseline TEXT,
    PRIMARY KEY (user_id, id)
);
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
_sqlite_ready = False


def get_conn():
    """Open a DB connection (local SQLite, or Azure SQL).

    When SQLITE_DB_PATH is set we open a local SQLite file (built into Python -
    no install/network/driver needed), ideal for offline testing. Otherwise we
    open Azure SQL via mssql_python + SQL_CONNECTION_STRING. Both are DB-API 2.0
    with '?' placeholders, so every query below works unchanged on either.

    A new connection per request keeps things thread-safe (FastAPI runs sync
    endpoints on a threadpool).

    NOTE: SQLite is for TESTING only. On a Function App the file lives on the
    per-instance temp disk, so it is wiped on restart/scale and not shared
    across instances. Use Azure SQL for anything that must persist.
    """
    if SQLITE_DB_PATH:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        global _sqlite_ready
        if not _sqlite_ready:
            conn.executescript(_SQLITE_SCHEMA)
            conn.commit()
            _sqlite_ready = True
        return conn

    conn_str = os.environ.get("SQL_CONNECTION_STRING")
    if not conn_str:
        logger.error("SQL_CONNECTION_STRING not configured")
        raise HTTPException(status_code=500, detail="SQL not configured")

    # lazy import: only needed for Azure SQL, so SQLite mode runs without the
    # driver installed. Add `mssql-python` to requirements.txt for deployment.
    try:
        import mssql_python
    except ImportError:
        logger.exception("mssql_python driver not installed")
        raise HTTPException(status_code=500, detail="SQL driver not available")

    try:
        return mssql_python.connect(conn_str)
    except Exception:
        logger.exception("Azure SQL connection failed")
        raise HTTPException(status_code=503, detail="Database unavailable")


def _fetchone_dict(cur) -> Optional[dict]:
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


def _fetchall_dicts(cur) -> list:
    """Return all rows as a list of {column_name: value} dicts (see _fetchone_dict).

    Empty list when the query matched nothing.
    """
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
