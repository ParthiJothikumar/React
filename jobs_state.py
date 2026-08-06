"""Read/write the async-job columns on the conversation row.

We reuse the existing `conversations` table (a job belongs to a conversation) instead
of a separate table: the worker gets the job_id from the queue message (no need to
scan for running jobs), and the FE reads status by conversation_id -- both of which
this row already supports. These functions touch ONLY the job_* columns via targeted
UPDATEs, so they never clash with the flow's full-row save in persistence.py (which
does not name the job_* columns).

Every function takes an open DB connection (like persistence.py) so it works the same
on SQLite and Azure SQL, with '?' placeholders.
"""
from datetime import datetime, timezone

from app.db import _fetchone_dict

# Job status values written to conversations.job_status.
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_job_record(conn, user_id: str, conv_id: str, job_id: str) -> None:
    """Mark the conversation's job as just-started (running)."""
    conn.cursor().execute(
        "UPDATE conversations SET job_id = ?, job_status = ?, job_progress = ?, "
        "job_result = NULL, updated_at = ? WHERE user_id = ? AND id = ?",
        (job_id, JOB_RUNNING, "Starting...", _now(), user_id, conv_id),
    )
    conn.commit()


def update_progress(conn, user_id: str, conv_id: str, progress: str) -> None:
    """Store the latest progress line while the job is still running."""
    conn.cursor().execute(
        "UPDATE conversations SET job_status = ?, job_progress = ?, updated_at = ? "
        "WHERE user_id = ? AND id = ?",
        (JOB_RUNNING, progress, _now(), user_id, conv_id),
    )
    conn.commit()


def finish_job(conn, user_id: str, conv_id: str, result: str) -> None:
    """Mark the job done and store the final result text."""
    conn.cursor().execute(
        "UPDATE conversations SET job_status = ?, job_progress = ?, job_result = ?, "
        "updated_at = ? WHERE user_id = ? AND id = ?",
        (JOB_DONE, "Complete.", result, _now(), user_id, conv_id),
    )
    conn.commit()


def fail_job(conn, user_id: str, conv_id: str, reason: str) -> None:
    """Mark the job failed (timed out / error) with a reason for the FE."""
    conn.cursor().execute(
        "UPDATE conversations SET job_status = ?, job_progress = ?, updated_at = ? "
        "WHERE user_id = ? AND id = ?",
        (JOB_FAILED, reason, _now(), user_id, conv_id),
    )
    conn.commit()


def get_job(conn, user_id: str, conv_id: str) -> dict:
    """Return {job_id, job_status, job_progress, job_result} for a conversation."""
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id, job_status, job_progress, job_result "
        "FROM conversations WHERE user_id = ? AND id = ?",
        (user_id, conv_id),
    )
    return _fetchone_dict(cur) or {}
