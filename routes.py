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
    _conversation_messages,
    _ensure_state,
    create_conversation,
)
from app.clients.multilingual import translate_messages
from app.config import DONE, SQLITE_DB_PATH, logger
from app.db import _fetchall_dicts, get_conn
from app.flow import step
from app.persistence import (
    load_conversation,
    load_session,
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
        save_session(
            conn, request.user_id, session_id, conversation.id, request.message
        )
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
_CONV_COLS = (
    "id, conversation_id, stage, question, answer, session_id, seq, "
    "title, created_at, updated_at"
)
_SQL_CONVERSATIONS_BY_SESSION = (
    "SELECT " + _CONV_COLS + " FROM conversations "
    "WHERE user_id = ? AND session_id = ? "
    "AND (stage IS NULL OR stage <> 'DONE') ORDER BY seq ASC"
)
_SQL_CONVERSATIONS_RECENT_SQLITE = (
    "SELECT " + _CONV_COLS + " FROM conversations "
    "WHERE user_id = ? AND (stage IS NULL OR stage <> 'DONE') "
    "ORDER BY created_at DESC LIMIT 10"
)
_SQL_CONVERSATIONS_RECENT_AZURE = (
    "SELECT TOP 10 " + _CONV_COLS + " FROM conversations "
    "WHERE user_id = ? AND (stage IS NULL OR stage <> 'DONE') "
    "ORDER BY created_at DESC"
)


@router.get("/conversations/{user_id}")
def get_conversation(user_id: str, session_id: Optional[str] = None):
    """Return the user's conversations, each with its Foundry messages attached.

    Pass ?session_id=... to get just one session's chain (ordered by seq); omit it
    for the user's most recent conversations. SQL supplies the conv_ids; Foundry
    supplies the messages inside each (one items.list per conversation).
    """
    openai_client = _ensure_state()["openai_client"]
    conn = get_conn()
    try:
        cur = conn.cursor()
        # Step 1: get the conversation rows (conv_ids) from SQL using the pre-built
        # constant statements above. Exclude ended (DONE) conversations so they
        # drop out of history.
        if session_id:
            cur.execute(_SQL_CONVERSATIONS_BY_SESSION, (user_id, session_id))
        elif SQLITE_DB_PATH:
            cur.execute(_SQL_CONVERSATIONS_RECENT_SQLITE, (user_id,))
        else:
            cur.execute(_SQL_CONVERSATIONS_RECENT_AZURE, (user_id,))
        rows = _fetchall_dicts(cur)
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")
    finally:
        conn.close()

    # Step 2: attach each conversation's messages from Foundry
    for row in rows:
        try:
            row["messages"] = _conversation_messages(openai_client, row["conversation_id"])
            row["messages_ok"] = True
        except Exception:
            logger.exception("load messages failed conv_id=%s", row.get("conversation_id"))
            row["messages"] = []
            row["messages_ok"] = False  # FE can flag "couldn't load this part"

    return {"user_id": user_id, "session_id": session_id, "conversations": rows}
