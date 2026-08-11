"""HTTP endpoints (served under the /workflow mount).

The endpoints live on an APIRouter that main.py attaches to the FastAPI app. Each one
is thin: it wires together the flow engine (flow.step), persistence, the Foundry client
and translation, then shapes the JSON the frontend expects. Routes are defined WITHOUT
the /workflow prefix -- http_app/ mounts this app under /workflow, so e.g. POST /chat is
reached as POST /workflow/chat (unchanged from before the split).
"""
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.clients.foundry import (
    _ensure_state,
    create_conversation,
)
from app.clients.multilingual import translate_messages
from app.config import (
    DIAGNOSTICS_RUNNING,
    DIAGNOSTICS_STATUS_URL,
    DONE,
    JOB_KIND_DIAGNOSTIC,
    JOB_KIND_TROUBLESHOOT,
    JOB_MAX_POLLS,
    JOB_STATUS_RETRIES,
    SQLITE_DB_PATH,
    TROUBLESHOOT_RUNNING,
    TROUBLESHOOT_STATUS_URL,
    logger,
)
from app.clients.jobs_agent import check_status, job_baseline_stamp, start_job
from app.db import _fetchall_dicts, get_conn
from app.flow import diagnostics_completed, step, troubleshoot_completed
from app.jobs_state import (
    fail_job,
    finish_job,
    get_job,
    start_job_record,
    update_progress,
)
from app.persistence import (
    append_turns,
    load_conversation,
    load_session,
    load_turns,
    save_conversation,
    save_session,
)
from app.schemas import ChatRequest, ContinueChatRequest

router = APIRouter()


@router.get("/")
def read_root():
    """Health-check root endpoint; returns a static service banner."""
    return {"message": "IT Support Orchestrator API"}


FALLBACK_MESSAGE = (
    "Sorry, something went wrong on our side and I couldn't process that just now. "
    "Please try again in a moment."
)


def _fallback_chat_response(user_id, session_id=None, conversation_id=None):
    """Safe chat payload returned when a turn fails unexpectedly.

    Shaped like a normal /chat response so the frontend renders it as an assistant
    bubble (with error=True) instead of choking on a raw HTTP 500. Client errors
    (404/422) are still raised normally so the FE can handle them explicitly.
    """
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": "ERROR",
        "done": True,
        "error": True,
        "messages": [FALLBACK_MESSAGE],
        "answer": FALLBACK_MESSAGE,
    }



def _dispatch_pending_job(conn, user_id, conv_id, pending):
    """Start an async job that flow.step() requested (via flow_vars["start_job"]).

    No queue, no background loop: we just call the agent's /start to get a job_id and
    record it on the conversation row (job_status=running). From then on the FE polls
    GET /jobs/status, which calls the agent's /status live and advances the flow when
    it finishes. Runs AFTER the conversation row is saved, so the row exists to update.
    """
    if not pending:
        return
    job_id = start_job(
        pending["kind"], pending.get("start_url", ""), conv_id, pending.get("input", "")
    )
    # Capture the baseline at trigger time so a later poll can tell OUR fresh Intune
    # run-state row apart from a stale one left by a previous run.
    start_job_record(conn, user_id, conv_id, job_id, job_baseline_stamp())


@router.post("/chat")
def chat(request: ChatRequest):
    """Start a new chat session and run the first turn.

    Creates a fresh Foundry conversation and a new session id, runs step() for the
    opening message, translates the reply into the user's language, then persists
    the conversation and session rows. Returns the turn's messages + stage. On a
    server-side failure returns a fallback message instead of a raw 500.
    """
    _ensure_state()
    conn = get_conn()
    try:
        session_id = "sess_" + uuid.uuid4().hex
        conversation = create_conversation()
        messages, stage, flow_vars = step(
            conversation.id, request.message, None, {"user_id": request.user_id}
        )
        # if step() asked to start an async job, pull the request out before saving
        # (so it isn't persisted in vars); we dispatch it once the row exists.
        pending_job = flow_vars.pop("start_job", None)
        messages = translate_messages(messages, flow_vars.get("lang"))
        answer = "\n\n".join(msg for msg in messages if msg)
        save_conversation(
            conn,
            request.user_id,
            conversation.id,
            stage=stage,
            flow_vars=flow_vars,
            question=request.message,
            answer=answer,
            session_id=session_id,
            seq=0,
        )
        # Record the FE-visible transcript for this turn (Approach A): the user's
        # message plus each assistant line we're returning, exactly as shown.
        append_turns(
            conn,
            request.user_id,
            conversation.id,
            [("user", request.message)] + [("assistant", m) for m in messages],
        )
        save_session(
            conn, request.user_id, session_id, conversation.id, request.message
        )
        _dispatch_pending_job(conn, request.user_id, conversation.id, pending_job)
        return {
            "user_id": request.user_id,
            "session_id": session_id,
            "conversation_id": conversation.id,
            "stage": stage,
            "done": stage == DONE,
            "messages": messages,
            "answer": answer,
        }
    except HTTPException as exc:
        if exc.status_code < 500:
            raise  # client errors (404/422) -> let the frontend handle them
        logger.error("chat server error user_id=%s: %s", request.user_id, exc.detail)
        return _fallback_chat_response(request.user_id)
    except Exception:
        logger.exception("chat failed user_id=%s", request.user_id)
        return _fallback_chat_response(request.user_id)
    finally:
        conn.close()


@router.post("/chat/continue")
def continue_chat(request: ContinueChatRequest):
    """Continue an ACTIVE conversation in a session by session_id.

    Loads the session's current conversation and advances it by one turn. If that
    conversation has already ended (stage=DONE) it is final -- we return 409 so the
    frontend starts a fresh chat (POST /chat); there is no rollover and no memory
    carry-over. Translates outgoing messages, persists state, and returns a fallback
    message on server-side failure.
    """
    _ensure_state()
    conn = get_conn()
    try:
        session_id = request.session_id

        if session_id is None:
            raise HTTPException(
                status_code=422, detail="session_id or conversation_id is required"
            )

        session = load_session(conn, request.user_id, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        conv_id = session.get("current_conversation_id")
        conversation = load_conversation(conn, request.user_id, conv_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        stage = conversation.get("stage", DONE)
        flow_vars = conversation.get("vars", {})

        # Ended conversations are final -- no rollover, no memory carry-over. The
        # frontend should start a new chat (POST /chat) instead of continuing.
        if stage == DONE:
            raise HTTPException(
                status_code=409,
                detail="This conversation has ended. Please start a new chat.",
            )

        # normal in-flow turn (active conversation)
        messages, new_stage, new_flow_vars = step(
            conv_id, request.message, stage, flow_vars
        )
        pending_job = new_flow_vars.pop("start_job", None)
        messages = translate_messages(messages, new_flow_vars.get("lang"))
        answer = "\n\n".join(msg for msg in messages if msg)
        save_conversation(
            conn,
            request.user_id,
            conv_id,
            stage=new_stage,
            flow_vars=new_flow_vars,
            question=request.message,
            answer=answer,
        )
        append_turns(
            conn,
            request.user_id,
            conv_id,
            [("user", request.message)] + [("assistant", m) for m in messages],
        )
        _dispatch_pending_job(conn, request.user_id, conv_id, pending_job)

        return {
            "user_id": request.user_id,
            "session_id": session_id,
            "conversation_id": conv_id,
            "stage": new_stage,
            "done": new_stage == DONE,
            "messages": messages,
            "answer": answer,
        }
    except HTTPException as exc:
        if exc.status_code < 500:
            raise  # client errors (404/422) -> let the frontend handle them
        logger.error(
            "continue_chat server error session_id=%s: %s",
            request.session_id, exc.detail,
        )
        return _fallback_chat_response(request.user_id, session_id=request.session_id)
    except Exception:
        logger.exception("continue_chat failed session_id=%s", request.session_id)
        return _fallback_chat_response(request.user_id, session_id=request.session_id)
    finally:
        conn.close()


def _job_payload(user_id, session_id, conversation_id, stage, *, done, messages, answer=None):
    """Shape the JSON the FE spinner expects (consistent across every return path)."""
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": done,
        "messages": messages,
        "answer": answer if answer is not None else "\n\n".join(m for m in messages if m),
    }


@router.get("/jobs/status")
def job_status(user_id: str, conversation_id: str, session_id: Optional[str] = None):
    """The FE spinner polls this every ~30s (no backend queue/loop).

    On each poll we look up the job_id by conversation_id, call the agent's /status
    LIVE, store the latest status in the DB, and:
      - still running -> return the progress with done=False (keep spinning)
      - done/failed   -> advance the flow ONCE (result + next question), persist the
                         new stage, and return done=True so the FE stops polling.
    """
    conn = get_conn()
    try:
        conversation = load_conversation(conn, user_id, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        stage = conversation.get("stage")
        flow_vars = conversation.get("vars", {})
        job = get_job(conn, user_id, conversation_id)

        # Not in a running stage -> nothing to poll (not started, or already advanced).
        if stage not in (DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING):
            return _job_payload(
                user_id, session_id, conversation_id, stage,
                done=job.get("job_status") in ("done", "failed"),
                messages=[job.get("job_result") or job.get("job_progress") or "No active job."],
            )

        # Running -> which agent to ask, from the stage.
        if stage == DIAGNOSTICS_RUNNING:
            kind, status_url = JOB_KIND_DIAGNOSTIC, DIAGNOSTICS_STATUS_URL
        else:
            kind, status_url = JOB_KIND_TROUBLESHOOT, TROUBLESHOOT_STATUS_URL

        polls = flow_vars.get("job_polls", 0)

        # Within this ONE poll, try the status check up to JOB_STATUS_RETRIES times so a
        # transient blip doesn't waste a poll. If every attempt fails, treat this poll as
        # "still running" -- it just counts toward the JOB_MAX_POLLS cap below.
        status = None
        for _ in range(JOB_STATUS_RETRIES):
            try:
                status = check_status(kind, status_url, job.get("job_id"), polls,
                                      job.get("job_baseline") or "")  # LIVE call
                break
            except Exception:
                logger.exception("check_status attempt failed conv_id=%s", conversation_id)
        if status is None:
            status = {"state": "running",
                      "progress": job.get("job_progress") or "Working...", "result": None}

        state = status.get("state", "running")

        # Still running -> count this poll; give up once we've hit JOB_MAX_POLLS.
        if state not in ("done", "failed"):
            polls += 1
            if polls >= JOB_MAX_POLLS:  # cap reached -> give up
                status = {"state": "failed",
                          "progress": "No result after several checks. Your ticket stays open.",
                          "result": None}
                state = "failed"
            else:
                update_progress(conn, user_id, conversation_id,
                                progress=status.get("progress") or "Working...")
                flow_vars["job_polls"] = polls
                save_conversation(conn, user_id, conversation_id, stage=stage,
                                  flow_vars=flow_vars, question="[job-poll]",
                                  answer=status.get("progress") or "")
                return _job_payload(
                    user_id, session_id, conversation_id, stage,
                    done=False, messages=[status.get("progress") or "Working..."],
                )

        # Finished (done or failed) -> record it, then advance the flow once.
        if state == "done":
            finish_job(conn, user_id, conversation_id, result=status.get("result") or "Complete.")
        else:
            fail_job(conn, user_id, conversation_id, reason=status.get("progress") or "Failed.")
        job = get_job(conn, user_id, conversation_id)  # refresh with final status

        messages: list[str] = []
        if stage == DIAGNOSTICS_RUNNING:
            messages, new_stage, flow_vars = diagnostics_completed(
                conversation_id, flow_vars, messages, job
            )
        else:  # TROUBLESHOOT_RUNNING
            messages, new_stage, flow_vars = troubleshoot_completed(
                conversation_id, flow_vars, messages, job
            )

        messages = translate_messages(messages, flow_vars.get("lang"))
        answer = "\n\n".join(m for m in messages if m)
        save_conversation(conn, user_id, conversation_id, stage=new_stage,
                          flow_vars=flow_vars, question="[job-poll]", answer=answer)
        # Job completion produces assistant-only lines (result + next question).
        # No user turn here -- the FE was polling, not chatting.
        append_turns(
            conn, user_id, conversation_id,
            [("assistant", m) for m in messages],
        )
        return _job_payload(
            user_id, session_id, conversation_id, new_stage,
            done=True, messages=messages, answer=answer,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("job_status failed conv_id=%s", conversation_id)
        raise HTTPException(status_code=500, detail="Failed to read job status")
    finally:
        conn.close()


@router.get("/sessions/{user_id}")
def get_sessions(user_id: str):
    """List a user's most recent sessions (up to 20), newest first.

    Sidebar data: session id, current conversation, title, timestamps. Messages
    are not included -- fetch those per session via /conversations/{user_id}.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        # Exclude sessions whose current conversation has ended (stage = DONE), so
        # ended chats drop off the left panel.
        if SQLITE_DB_PATH:
            cur.execute(
                "SELECT s.id, s.session_id, s.current_conversation_id, s.title, "
                "s.created_at, s.updated_at FROM sessions s "
                "LEFT JOIN conversations c "
                "  ON c.user_id = s.user_id AND c.id = s.current_conversation_id "
                "WHERE s.user_id = ? AND (c.stage IS NULL OR c.stage <> 'DONE') "
                "ORDER BY s.updated_at DESC LIMIT 20",
                (user_id,),
            )
        else:
            cur.execute(
                "SELECT TOP 20 s.id, s.session_id, s.current_conversation_id, s.title, "
                "s.created_at, s.updated_at FROM sessions s "
                "LEFT JOIN conversations c "
                "  ON c.user_id = s.user_id AND c.id = s.current_conversation_id "
                "WHERE s.user_id = ? AND (c.stage IS NULL OR c.stage <> 'DONE') "
                "ORDER BY s.updated_at DESC",
                (user_id,),
            )
        rows = _fetchall_dicts(cur)
        return {"sessions": rows}
    except Exception:
        logger.exception("get_sessions failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load sessions")
    finally:
        conn.close()


# Conversation SELECT columns kept as one constant so the three query variants
# (by-session / recent-SQLite / recent-Azure) stay in sync. The full statements
# are assembled from that constant here -- NOT with f-strings at the execute call
# -- so static analysis (Snyk) doesn't flag SQL built by string formatting.
# _CONV_COLS is a fixed literal, never user input; all user values use ? binding.
_CONV_COLS = "conversation_id, session_id, seq"
_SQL_CONVERSATIONS_BY_SESSION = (
    "SELECT " + _CONV_COLS + " FROM conversations "
    "WHERE user_id = ? AND session_id = ? "
    "AND (stage IS NULL OR stage <> 'DONE') ORDER BY seq ASC"
)


@router.get("/conversations/{user_id}")
def get_conversation(user_id: str, session_id: Optional[str] = None):
    """Return the session's messages as a flat list of {role, content}.

    `conversations` is every turn we showed the user (from conversation_turns,
    Approach A), concatenated in order -- no other conversation metadata.
    `session_id` is required; a request without it gets a 400.
    """
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    conn = get_conn()
    try:
        cur = conn.cursor()
        # Step 1: get this session's conversations, excluding ended (DONE) ones.
        cur.execute(_SQL_CONVERSATIONS_BY_SESSION, (user_id, session_id))
        rows = _fetchall_dicts(cur)
        # Step 2: flatten every conversation's turns into one list of {role, content}.
        conversations = []
        for row in rows:
            conversations.extend(load_turns(conn, user_id, row["conversation_id"]))
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")
    finally:
        conn.close()

    return {"user_id": user_id, "session_id": session_id, "conversations": conversations}
