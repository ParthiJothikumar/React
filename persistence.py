"""Persistence: conversations + sessions tables (Azure SQL / SQLite).

Each function takes an open DB connection (from db.get_conn) as its first argument, so
this module stays free of connection/lifecycle concerns and works identically against
SQLite and Azure SQL. Handlers upsert with an UPDATE-then-INSERT-on-miss pattern that
avoids dialect-specific MERGE/UPSERT syntax.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from app.db import _fetchone_dict


def load_conversation(conn, user_id: str, conv_id: str):
    """Load one conversation's saved state row, or None if it doesn't exist.

    Returns a dict keyed by column name, with `vars` already parsed from its JSON
    text back into a Python dict (empty dict when absent).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, conversation_id, stage, vars, question, answer, "
        "session_id, seq, title, created_at, updated_at "
        "FROM conversations WHERE user_id = ? AND id = ?",
        (user_id, conv_id),
    )
    row = _fetchone_dict(cur)
    if row is None:
        return None
    row["vars"] = json.loads(row["vars"]) if row.get("vars") else {}
    return row


def upsert_conversation(conn, item: dict) -> None:
    """Insert or update a conversation row (an "upsert").

    Serializes `vars` to JSON, then tries an UPDATE by primary key (user_id + id);
    if it matched no row (rowcount == 0), INSERTs instead. One code path that works
    on both SQLite and Azure SQL.
    """
    vars_val = item.get("vars")
    vars_json = (
        json.dumps(vars_val, default=str)
        if isinstance(vars_val, (dict, list))
        else vars_val
    )

    cur = conn.cursor()
    cur.execute(
        "UPDATE conversations SET conversation_id = ?, stage = ?, vars = ?, "
        "question = ?, answer = ?, session_id = ?, seq = ?, "
        "title = ?, created_at = ?, updated_at = ? WHERE user_id = ? AND id = ?",
        (
            item.get("conversation_id"),
            item.get("stage"),
            vars_json,
            item.get("question"),
            item.get("answer"),
            item.get("session_id"),
            item.get("seq"),
            item.get("title"),
            item.get("created_at"),
            item.get("updated_at"),
            item.get("user_id"),
            item.get("id"),
        ),
    )
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO conversations (id, user_id, conversation_id, stage, vars, "
            "question, answer, session_id, seq, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("id"),
                item.get("user_id"),
                item.get("conversation_id"),
                item.get("stage"),
                vars_json,
                item.get("question"),
                item.get("answer"),
                item.get("session_id"),
                item.get("seq"),
                item.get("title"),
                item.get("created_at"),
                item.get("updated_at"),
            ),
        )
    conn.commit()


def save_conversation(
    conn,
    user_id: str,
    conv_id: str,
    *,
    stage: str,
    flow_vars: dict,
    question: str,
    answer: str,
    session_id: Optional[str] = None,
    seq: Optional[int] = None,
) -> None:
    """Save a conversation turn: merge the given fields with any existing row.

    Lineage fields (session_id, seq) fall back to whatever is already stored when
    passed as None, so a normal turn need not re-supply them. On first save it
    stamps created_at and title (= the question), then delegates the actual write
    to upsert_conversation.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = load_conversation(conn, user_id, conv_id) or {}
    item = {
        "id": conv_id,
        "user_id": user_id,
        "conversation_id": conv_id,
        "stage": stage,
        "vars": flow_vars,
        "question": question,
        "answer": answer,
        "updated_at": now,
        # lineage: use the passed value, else keep what's already stored
        "session_id": session_id if session_id is not None else existing.get("session_id"),
        "seq": seq if seq is not None else existing.get("seq", 0),
    }
    if not existing:
        item["title"] = question
        item["created_at"] = now
    else:
        item["title"] = existing.get("title")
        item["created_at"] = existing.get("created_at", now)
    upsert_conversation(conn, item)


def load_session(conn, user_id: str, session_id: str):
    """Load one session row (by user_id + session_id) as a dict, or None if
    not found."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, session_id, user_id, current_conversation_id, title, "
        "created_at, updated_at FROM sessions WHERE user_id = ? AND id = ?",
        (user_id, session_id),
    )
    return _fetchone_dict(cur)


def upsert_session(conn, item: dict) -> None:
    """Insert or update a session row -- same update-then-insert upsert as
    upsert_conversation, but for the sessions table."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE sessions SET session_id = ?, current_conversation_id = ?, "
        "title = ?, created_at = ?, updated_at = ? WHERE user_id = ? AND id = ?",
        (
            item.get("session_id"),
            item.get("current_conversation_id"),
            item.get("title"),
            item.get("created_at"),
            item.get("updated_at"),
            item.get("user_id"),
            item.get("id"),
        ),
    )
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO sessions (id, session_id, user_id, current_conversation_id, "
            "title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("id"),
                item.get("session_id"),
                item.get("user_id"),
                item.get("current_conversation_id"),
                item.get("title"),
                item.get("created_at"),
                item.get("updated_at"),
            ),
        )
    conn.commit()


def save_session(
    conn, user_id: str, session_id: str, current_conversation_id: str, title: str
) -> None:
    """Save/refresh a session: point it at the current conversation and bump
    updated_at, preserving the original created_at and title. Delegates the write
    to upsert_session.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = load_session(conn, user_id, session_id) or {}
    item = {
        "id": session_id,
        "session_id": session_id,
        "user_id": user_id,
        "current_conversation_id": current_conversation_id,
        "updated_at": now,
        "created_at": existing.get("created_at", now),
        "title": existing.get("title", title),
    }
    upsert_session(conn, item)
