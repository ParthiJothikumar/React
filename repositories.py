"""Repositories: all SQL for the conversations, sessions and transcript tables.

Each repository is constructed with an open connection and owns the queries for one
table group. Holding the connection is the reason these are classes -- it stops every
call site threading `conn` (and usually `user_id`) through as parameters, and it gives
the transaction boundary somewhere to live.

Both repositories work identically against SQLite and Azure SQL: every statement uses
'?' placeholders, and the upserts use an UPDATE-then-INSERT-on-miss pattern instead of
dialect-specific MERGE/UPSERT syntax.

The SELECT column lists are kept as class constants so the dialect variants stay in
sync, and the full statements are assembled from them at class level -- never with an
f-string at the execute() call -- so static analysis (Snyk) doesn't flag SQL built by
string formatting. Those constants are fixed literals, never user input; every user
value is bound with ?.

COMMITS: a write method calls self._commit(), which commits immediately when used on
its own but defers to the enclosing transaction() block when there is one. The service
layer wraps the sequences that must agree with each other -- completing a job, and
persisting a chat turn -- so those commit exactly once instead of two or three times.

advance_after_job() is the other half of that: it folds the job outcome and the new
stage into ONE conditional UPDATE, which doubles as the compare-and-swap that stops
two concurrent pollers both completing the same job.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from app.constants import DIAGNOSTICS_RUNNING, JOB_RUNNING, TROUBLESHOOT_RUNNING
from app.db import fetchall_dicts, fetchone_dict


def _now() -> str:
    """Current time as an ISO-8601 UTC string (how every timestamp column is stored)."""
    return datetime.now(timezone.utc).isoformat()


def _vars_json(vars_val):
    """Serialize flow_vars for the `vars` column (already-serialized values pass through)."""
    return (
        json.dumps(vars_val, default=str)
        if isinstance(vars_val, (dict, list))
        else vars_val
    )


class BaseRepository:
    """Shared connection and commit handling for the repositories."""

    def __init__(self, conn):
        self._conn = conn
        self._in_transaction = False

    def _cursor(self):
        return self._conn.cursor()

    def _commit(self) -> None:
        """Commit this write -- unless we are inside a transaction() block.

        Every write method calls this instead of conn.commit() directly, so a single
        write still commits on its own (as before), but a group of writes wrapped in
        transaction() commits exactly once, at the end.
        """
        if not self._in_transaction:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Group several writes into ONE commit -- all of them, or none.

        Used by the service layer around the write sequences that must agree with each
        other (completing a job; persisting a chat turn). Without it, each write
        committed separately, so a crash part-way through left the conversation row
        advanced but its transcript rows missing.

        Open this as LATE as possible and keep it short: an open transaction holds
        locks on the row, so it must never wrap an agent HTTP call.
        """
        if self._in_transaction:
            yield self  # already inside one -- the outermost block owns the commit
            return
        self._in_transaction = True
        try:
            yield self
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False


class ConversationRepository(BaseRepository):
    """conversations + conversation_turns: flow state, job state and the transcript.

    Takes `use_sqlite` because find_running() needs a row limit, and that is the one
    thing the two dialects spell differently (LIMIT vs TOP).

    The job_* columns live on the conversation row (a job belongs to a conversation)
    rather than in a separate table: the FE reads job status by conversation_id, which
    this row already supports. The job methods touch ONLY the job_* columns via
    targeted UPDATEs, so they never clash with save() (which does not name them).
    """

    _COLS = (
        "id, user_id, conversation_id, stage, vars, question, answer, "
        "session_id, seq, title, created_at, updated_at"
    )
    _SQL_LOAD = (
        "SELECT " + _COLS + " FROM conversations WHERE user_id = ? AND id = ?"
    )
    _SQL_UPDATE = (
        "UPDATE conversations SET conversation_id = ?, stage = ?, vars = ?, "
        "question = ?, answer = ?, session_id = ?, seq = ?, "
        "title = ?, created_at = ?, updated_at = ? WHERE user_id = ? AND id = ?"
    )
    _SQL_INSERT = (
        "INSERT INTO conversations (id, user_id, conversation_id, stage, vars, "
        "question, answer, session_id, seq, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    _SQL_BY_SESSION = (
        "SELECT conversation_id, session_id, seq FROM conversations "
        "WHERE user_id = ? AND session_id = ? "
        "AND (stage IS NULL OR stage <> 'DONE') ORDER BY seq ASC"
    )
    _SQL_JOB = (
        "SELECT job_id, job_status, job_message, job_baseline, job_started_at "
        "FROM conversations WHERE user_id = ? AND id = ?"
    )
    # Rows the reaper must look at: a job still marked running, on a conversation still
    # parked in a RUNNING stage, that NOBODY HAS TOUCHED for a while.
    #
    # That last condition is what keeps the reaper a backstop instead of a second poller.
    # updated_at is bumped by every poll, so a browser polling every ~30s keeps its own
    # conversation out of this result set. Without it the reaper re-checked every running
    # job on every sweep, duplicating work the user's own browser was already doing.
    #
    # ORDER BY updated_at, i.e. "whoever we have not looked at for the longest goes
    # first". This is what stops the batch limit starving anyone. Ordering by
    # job_started_at instead looked reasonable but never changed, so with more stuck jobs
    # than `limit` the same oldest-started rows were picked every sweep and the remainder
    # were never reached. Because a sweep bumps updated_at on everything it touches, this
    # ordering rotates by itself -- no extra state, and every job is eventually checked.
    _FIND_RUNNING_FROM = (
        "FROM conversations WHERE job_status = ? AND stage IN (?, ?) AND updated_at < ? "
        "ORDER BY updated_at ASC"
    )
    _SQL_FIND_RUNNING_SQLITE = (
        "SELECT user_id, id, job_started_at " + _FIND_RUNNING_FROM + " LIMIT ?"
    )
    _SQL_FIND_RUNNING_AZURE = (
        "SELECT TOP (?) user_id, id, job_started_at " + _FIND_RUNNING_FROM
    )
    _SQL_TURNS = (
        "SELECT role, content FROM conversation_turns "
        "WHERE user_id = ? AND conversation_id = ? ORDER BY seq ASC"
    )
    _SQL_MAX_SEQ = (
        "SELECT MAX(seq) FROM conversation_turns "
        "WHERE user_id = ? AND conversation_id = ?"
    )
    _SQL_INSERT_TURN = (
        "INSERT INTO conversation_turns "
        "(user_id, conversation_id, seq, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    # The compare-and-swap that makes job completion idempotent. Note the extra
    # `AND stage = ?` -- see advance_after_job().
    _SQL_ADVANCE_AFTER_JOB = (
        "UPDATE conversations SET stage = ?, vars = ?, question = ?, answer = ?, "
        "job_status = ?, job_message = ?, job_output = ?, "
        "updated_at = ? WHERE user_id = ? AND id = ? AND stage = ?"
    )
    # job_output is deliberately NOT selected here. get_job() feeds user-facing paths
    # (_already_advanced, and the "no active job" branch), and raw device output must
    # never reach the browser. Support reads it with load_job_output().
    _SQL_JOB_OUTPUT = (
        "SELECT job_output FROM conversations WHERE user_id = ? AND id = ?"
    )

    def __init__(self, conn, use_sqlite: bool = True):
        super().__init__(conn)
        self._use_sqlite = use_sqlite

    # -- flow state ---------------------------------------------------------
    def load(self, user_id: str, conv_id: str) -> Optional[dict]:
        """Load one conversation's saved state row, or None if it doesn't exist.

        Returns a dict keyed by column name, with `vars` already parsed from its JSON
        text back into a Python dict (empty dict when absent).
        """
        cur = self._cursor()
        cur.execute(self._SQL_LOAD, (user_id, conv_id))
        row = fetchone_dict(cur)
        if row is None:
            return None
        row["vars"] = json.loads(row["vars"]) if row.get("vars") else {}
        return row

    def save(
        self,
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
        stamps created_at and title (= the question), then upserts.
        """
        now = _now()
        existing = self.load(user_id, conv_id) or {}
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
            "session_id": (
                session_id if session_id is not None else existing.get("session_id")
            ),
            "seq": seq if seq is not None else existing.get("seq", 0),
            "title": question if not existing else existing.get("title"),
            "created_at": now if not existing else existing.get("created_at", now),
        }
        self._upsert(item)

    def _upsert(self, item: dict) -> None:
        """Insert or update a conversation row.

        Serializes `vars` to JSON, then tries an UPDATE by primary key (user_id + id);
        if it matched no row (rowcount == 0), INSERTs instead. One code path that works
        on both SQLite and Azure SQL.
        """
        vars_json = _vars_json(item.get("vars"))
        cur = self._cursor()
        cur.execute(
            self._SQL_UPDATE,
            (
                item.get("conversation_id"), item.get("stage"), vars_json,
                item.get("question"), item.get("answer"), item.get("session_id"),
                item.get("seq"), item.get("title"), item.get("created_at"),
                item.get("updated_at"), item.get("user_id"), item.get("id"),
            ),
        )
        if cur.rowcount == 0:
            cur.execute(
                self._SQL_INSERT,
                (
                    item.get("id"), item.get("user_id"), item.get("conversation_id"),
                    item.get("stage"), vars_json, item.get("question"),
                    item.get("answer"), item.get("session_id"), item.get("seq"),
                    item.get("title"), item.get("created_at"), item.get("updated_at"),
                ),
            )
        self._commit()

    def list_by_session(self, user_id: str, session_id: str) -> list:
        """Return the session's still-active conversations, oldest first."""
        cur = self._cursor()
        cur.execute(self._SQL_BY_SESSION, (user_id, session_id))
        return fetchall_dicts(cur)

    # -- transcript ---------------------------------------------------------
    def append_turns(self, user_id: str, conv_id: str, turns) -> None:
        """Append FE-visible messages to conversation_turns (Approach A).

        `turns` is a list of (role, content) pairs in display order -- typically the
        user's message followed by each assistant line we return that turn. This is the
        EXACT text shown to the user (post-translation, no agent JSON), so the
        conversations API can replay it verbatim. Blank/whitespace-only contents are
        skipped. Each row gets the next sequential seq for this conversation, so history
        always reads back in the order the user saw it.
        """
        rows = [(role, content) for role, content in turns if content and content.strip()]
        if not rows:
            return
        now = _now()
        cur = self._cursor()
        # Next seq = one past the current max for this conversation (0 on first write).
        cur.execute(self._SQL_MAX_SEQ, (user_id, conv_id))
        row = cur.fetchone()
        next_seq = (row[0] + 1) if row and row[0] is not None else 0
        for role, content in rows:
            cur.execute(
                self._SQL_INSERT_TURN,
                (user_id, conv_id, next_seq, role, content, now),
            )
            next_seq += 1
        self._commit()

    def load_turns(self, user_id: str, conv_id: str) -> list:
        """Return a conversation's FE-visible transcript as [{role, content}], oldest
        first. Empty list when nothing was recorded (e.g. chats created before Approach
        A)."""
        cur = self._cursor()
        cur.execute(self._SQL_TURNS, (user_id, conv_id))
        return [{"role": r[0], "content": r[1]} for r in cur.fetchall()]

    # -- job state (the job_* columns on the conversation row) --------------
    def start_job(
        self, user_id: str, conv_id: str, job_id: str, baseline: str = None
    ) -> None:
        """Mark the conversation's job as just-started (running).

        `baseline` is the trigger-time timestamp (see JobsClient.baseline_stamp);
        check_status uses it to tell OUR fresh result from a stale run-state row.
        """
        now = _now()
        self._cursor().execute(
            "UPDATE conversations SET job_id = ?, job_status = ?, job_message = ?, "
            "job_output = NULL, job_baseline = ?, job_started_at = ?, updated_at = ? "
            "WHERE user_id = ? AND id = ?",
            (job_id, JOB_RUNNING, "Starting...", baseline, now, now, user_id, conv_id),
        )
        self._commit()

    def find_running(self, limit: int, stale_before: str) -> list:
        """Running jobs that nobody has touched since `stale_before` -- the reaper's list.

        `stale_before` is an ISO-8601 UTC timestamp; anything updated more recently is
        assumed to have a browser watching it and is skipped. This only FINDS them; the
        caller decides what to do with each.
        """
        cur = self._cursor()
        # TOP (?) comes BEFORE the WHERE values in T-SQL; LIMIT ? comes after them.
        where = (JOB_RUNNING, DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING, stale_before)
        if self._use_sqlite:
            cur.execute(self._SQL_FIND_RUNNING_SQLITE, where + (limit,))
        else:
            cur.execute(self._SQL_FIND_RUNNING_AZURE, (limit,) + where)
        return fetchall_dicts(cur)

    def update_job_message(self, user_id: str, conv_id: str, message: str) -> None:
        """Record the progress line for one still-running poll. ONE column, ONE statement.

        This used to be two calls and two commits -- update_job_message + save() -- because
        the poll COUNTER lived inside vars. Expiry is now measured from job_started_at in
        wall-clock time, so there is no counter, nothing to write into vars, and nothing
        that can be left half-updated.

        It also does not touch question/answer. save() used to stamp question="[job-poll]"
        over the user's real last message on every poll. Nothing reads those columns -- the
        transcript lives in conversation_turns -- so it was pure noise.
        """
        self._cursor().execute(
            "UPDATE conversations SET job_status = ?, job_message = ?, updated_at = ? "
            "WHERE user_id = ? AND id = ?",
            (JOB_RUNNING, message, _now(), user_id, conv_id),
        )
        self._commit()

    def advance_after_job(
        self,
        user_id: str,
        conv_id: str,
        *,
        expected_stage: str,
        new_stage: str,
        flow_vars: dict,
        question: str,
        answer: str,
        job_status: str,
        job_message: str,
        job_output=None,
    ) -> bool:
        """Move the conversation off a RUNNING stage and record the job outcome.

        This is ONE conditional UPDATE, and that is the whole point. It replaces the
        old finish_job/fail_job + save pair, which wrote the job outcome and the new
        stage separately -- so a crash between them left job_status='done' with the
        stage never advanced.

        The `AND stage = ?` in the WHERE clause is a compare-and-swap: only the FIRST
        caller still sees the RUNNING stage and updates 1 row. A second poller -- two
        browser tabs, a refresh mid-poll, or another worker process -- updates 0 rows
        and must NOT advance the flow or write the transcript again.

        Returns True if this caller won the race.

        Call it inside transaction() together with append_turns, so the stage move and
        the transcript rows commit as one unit. A concurrent caller then blocks on the
        row lock and correctly sees 0 rows once the winner commits.
        """
        cur = self._cursor()
        cur.execute(
            self._SQL_ADVANCE_AFTER_JOB,
            (
                new_stage, _vars_json(flow_vars), question, answer,
                job_status, job_message, job_output, _now(),
                user_id, conv_id, expected_stage,
            ),
        )
        won = cur.rowcount == 1
        self._commit()
        return won

    def load_job_output(self, user_id: str, conv_id: str):
        """The raw device script output, for SUPPORT use only.

        Deliberately a separate method from get_job(): nothing on a user-facing path
        should be able to read this column by accident. If you call this, the value must
        not end up in an HTTP response.
        """
        cur = self._cursor()
        cur.execute(self._SQL_JOB_OUTPUT, (user_id, conv_id))
        row = cur.fetchone()
        return row[0] if row else None

    def get_job(self, user_id: str, conv_id: str) -> dict:
        """Return {job_id, job_status, job_message, job_baseline}.

        job_output is deliberately excluded -- see _SQL_JOB.
        """
        cur = self._cursor()
        cur.execute(self._SQL_JOB, (user_id, conv_id))
        return fetchone_dict(cur) or {}


class SessionRepository(BaseRepository):
    """sessions: the chat sessions shown in the frontend's left panel."""

    _COLS = (
        "id, session_id, user_id, current_conversation_id, title, "
        "created_at, updated_at"
    )
    _SQL_LOAD = "SELECT " + _COLS + " FROM sessions WHERE user_id = ? AND id = ?"
    _SQL_UPDATE = (
        "UPDATE sessions SET session_id = ?, current_conversation_id = ?, "
        "title = ?, created_at = ?, updated_at = ? WHERE user_id = ? AND id = ?"
    )
    _SQL_INSERT = (
        "INSERT INTO sessions (id, session_id, user_id, current_conversation_id, "
        "title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    # Sidebar list. Excludes sessions whose current conversation has ended
    # (stage = DONE), so ended chats drop off the left panel. The row limit is spelled
    # differently per dialect (LIMIT vs TOP), hence the two statements.
    _LIST_COLS = (
        "s.id, s.session_id, s.current_conversation_id, s.title, "
        "s.created_at, s.updated_at"
    )
    _LIST_FROM = (
        "FROM sessions s "
        "LEFT JOIN conversations c "
        "  ON c.user_id = s.user_id AND c.id = s.current_conversation_id "
        "WHERE s.user_id = ? AND (c.stage IS NULL OR c.stage <> 'DONE') "
    )
    _SQL_LIST_SQLITE = (
        "SELECT " + _LIST_COLS + " " + _LIST_FROM
        + "ORDER BY s.updated_at DESC LIMIT 20"
    )
    _SQL_LIST_AZURE = (
        "SELECT TOP 20 " + _LIST_COLS + " " + _LIST_FROM
        + "ORDER BY s.updated_at DESC"
    )

    def __init__(self, conn, use_sqlite: bool):
        super().__init__(conn)
        self._use_sqlite = use_sqlite

    def load(self, user_id: str, session_id: str) -> Optional[dict]:
        """Load one session row (by user_id + session_id) as a dict, or None."""
        cur = self._cursor()
        cur.execute(self._SQL_LOAD, (user_id, session_id))
        return fetchone_dict(cur)

    def save(
        self, user_id: str, session_id: str, current_conversation_id: str, title: str
    ) -> None:
        """Save/refresh a session: point it at the current conversation and bump
        updated_at, preserving the original created_at and title."""
        now = _now()
        existing = self.load(user_id, session_id) or {}
        self._upsert(
            {
                "id": session_id,
                "session_id": session_id,
                "user_id": user_id,
                "current_conversation_id": current_conversation_id,
                "updated_at": now,
                "created_at": existing.get("created_at", now),
                "title": existing.get("title", title),
            }
        )

    def _upsert(self, item: dict) -> None:
        """Insert or update a session row -- same update-then-insert as the
        conversation upsert, but for the sessions table."""
        cur = self._cursor()
        cur.execute(
            self._SQL_UPDATE,
            (
                item.get("session_id"), item.get("current_conversation_id"),
                item.get("title"), item.get("created_at"), item.get("updated_at"),
                item.get("user_id"), item.get("id"),
            ),
        )
        if cur.rowcount == 0:
            cur.execute(
                self._SQL_INSERT,
                (
                    item.get("id"), item.get("session_id"), item.get("user_id"),
                    item.get("current_conversation_id"), item.get("title"),
                    item.get("created_at"), item.get("updated_at"),
                ),
            )
        self._commit()

    def list_recent(self, user_id: str) -> list:
        """A user's most recent still-active sessions (up to 20), newest first."""
        cur = self._cursor()
        cur.execute(
            self._SQL_LIST_SQLITE if self._use_sqlite else self._SQL_LIST_AZURE,
            (user_id,),
        )
        return fetchall_dicts(cur)
