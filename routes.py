"""HTTP controllers (served under the /workflow mount).

These are the ONLY functions in the app that know HTTP exists, and each one does three
things and nothing else: validate the request shape, call ONE service method, and turn
the result -- or a domain error from errors.py -- into a response. All sequencing and
business policy lives in services.py and flow.py, so it stays testable and reusable
without a web server.

The service (and its per-request DB connection) arrives via Depends, so there is no
connection handling here either: deps.get_db() closes it after the response.

Routes are defined WITHOUT the /workflow prefix -- http_app/ mounts this app under
/workflow, so e.g. POST /chat is reached as POST /workflow/chat (unchanged).
"""
from fastapi import APIRouter, Depends, HTTPException

from app.config import logger
from app.constants import DONE
from app.deps import get_chat_service, get_history_service, get_job_service
from app.errors import (
    ConversationEnded,
    NotFound,
    TurnFailed,
    UpstreamUnavailable,
)
from app.schemas import (
    ChatRequest,
    ChatResponse,
    ContinueChatRequest,
    SessionListResponse,
    TranscriptResponse,
)
from app.services import ChatService, HistoryService, JobService

router = APIRouter()

FALLBACK_MESSAGE = (
    "Sorry, something went wrong on our side and I couldn't process that just now. "
    "Please try again in a moment."
)

# Used when a ServiceNow ticket was ALREADY raised before the turn failed. Telling that
# user to "try again" is what produced duplicate tickets: they would start over and a
# second incident would be opened for the same problem. Handing them the number and
# saying they need not start again removes the reason to retry.
ESCALATED_MESSAGE = (
    "Something went wrong on our side before I could finish. Your ticket {incident} has "
    "already been raised and the support team will follow up -- you can track it in "
    "ServiceNow. You don't need to start again."
)


def _fallback_chat_response(
    user_id, session_id=None, conversation_id=None, *, stage=DONE, done: bool = True
) -> dict:
    """Safe chat payload returned when a turn fails unexpectedly.

    The defaults describe a FIRST turn that failed (POST /chat): nothing was saved, so
    there is no conversation to continue and the FE must start fresh.

    /chat/continue passes the real stage with done=False, because there a failure leaves
    the conversation untouched and the user can just resend the same message. Claiming
    DONE there used to throw away a working conversation -- and produce a duplicate
    ServiceNow incident when the user started over.

    Shaped like a normal /chat response so the frontend renders it as an assistant
    bubble (with error=True) instead of choking on a raw HTTP 500. This is a
    presentation decision, which is why it lives here and not in services.py. Client
    errors (404/409/422) are still raised normally so the FE can handle them.

    stage is DONE, not a made-up "ERROR". Two reasons:
      * it agrees with done=True. A turn that failed left NOTHING saved -- no
        conversation row, and session_id is None -- so there is genuinely nothing to
        continue. DONE is the frontend's existing signal for "start a new chat", which
        is exactly the right recovery.
      * "ERROR" was not a real stage: absent from constants.py, impossible for flow.py
        to produce, and unhandled by it. A frontend keying on stage == "DONE" would
        have missed it and shown a dead conversation with no way forward.

    error=True is what distinguishes "failed" from "completed normally", so the FE shows
    the error wording rather than a normal ending. The FE should branch on done/error --
    never on the stage string, which is internal flow state.
    """
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "done": done,
        "error": True,
        "messages": [FALLBACK_MESSAGE],
        "answer": FALLBACK_MESSAGE,
    }


def _ended_chat_response(user_id, session_id, message: str) -> dict:
    """Payload for a conversation that has already finished.

    Deliberately NOT an error and NOT a 409. The conversation completed normally --
    there is simply nothing left to continue -- so error=False and the message is the
    domain error's own text, which is author-written and safe to display.

    Why a 200 rather than the 409 this used to be: the frontend needs done=True to reset
    itself and start a new chat. A 409 carries only {"detail": ...} -- no stage, no done
    flag, no messages -- so the FE would need a separate branch to recover, and an
    unhandled one leaves the user at a dead end. One response shape keeps the FE's
    rendering path single.
    """
    return {
        "user_id": user_id,
        "session_id": session_id,
        "conversation_id": None,
        "stage": DONE,
        "done": True,
        "error": False,
        "messages": [message],
        "answer": message,
    }


def _escalated_chat_response(user_id, incident_id) -> dict:
    """Failure payload for when a ticket already exists.

    Same shape as any other chat response, so the FE renders it as an assistant bubble.
    done=True because a failed first turn saved nothing -- there is no conversation to
    continue -- but the message deliberately does NOT ask the user to retry, because the
    work is already in ServiceNow's hands.

    error stays True: something did go wrong, and the FE should style it as such.
    """
    message = ESCALATED_MESSAGE.format(incident=incident_id)
    return {
        "user_id": user_id,
        "session_id": None,
        "conversation_id": None,
        "stage": DONE,
        "done": True,
        "error": True,
        "messages": [message],
        "answer": message,
    }


@router.get("/")
def read_root():
    """Health-check root endpoint; returns a static service banner."""
    return {"message": "IT Support Orchestrator API"}


@router.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    service: ChatService = Depends(get_chat_service),
):
    """Start a new chat session and run the first turn.

    On a server-side failure returns the fallback message instead of a raw 500.
    """
    try:
        return service.start_chat(payload.user_id, payload.message)
    except ConversationEnded as exc:
        # Unreachable today (a new chat starts at stage=None, which always has a
        # handler). Handled the same way as in /chat/continue so the two endpoints
        # behave identically if the flow ever gains a terminal first turn.
        return _ended_chat_response(payload.user_id, None, str(exc))
    except NotFound:
        raise  # client error -> 404 via main.py
    except TurnFailed as exc:
        logger.exception(
            "chat failed user_id=%s incident_id=%s", payload.user_id, exc.incident_id
        )
        if exc.incident_id:
            # A real ticket exists. Give them the number, don't invite a retry.
            return _escalated_chat_response(payload.user_id, exc.incident_id)
        # Nothing was created anywhere -- no ticket, no row, nothing in the history --
        # so a retry starts genuinely fresh and is the right thing to ask for.
        return _fallback_chat_response(payload.user_id)
    except Exception:
        logger.exception("chat failed user_id=%s", payload.user_id)
        return _fallback_chat_response(payload.user_id)


@router.post("/chat/continue", response_model=ChatResponse)
def continue_chat(
    payload: ContinueChatRequest,
    service: ChatService = Depends(get_chat_service),
):
    """Continue an ACTIVE conversation in a session by session_id."""
    if payload.session_id is None:
        raise HTTPException(
            status_code=422, detail="session_id or conversation_id is required"
        )
    try:
        return service.continue_chat(
            payload.user_id, payload.session_id, payload.message
        )
    except ConversationEnded as exc:
        # The conversation is finished -- not a failure. Return the normal shape with
        # done=True so the FE resets to a new chat, instead of a 409 it would have to
        # special-case. See _ended_chat_response.
        return _ended_chat_response(payload.user_id, payload.session_id, str(exc))
    except NotFound:
        raise  # the session/conversation genuinely doesn't exist -> 404 via main.py
    except TurnFailed as exc:
        # The conversation survived -- report its real stage with done=False so the FE
        # keeps it and the user can resend the same message.
        logger.exception(
            "continue_chat failed session_id=%s stage=%s",
            payload.session_id, exc.stage,
        )
        return _fallback_chat_response(
            payload.user_id,
            session_id=payload.session_id,
            conversation_id=exc.conversation_id,
            stage=exc.stage,
            done=False,
        )
    except Exception:
        logger.exception("continue_chat failed session_id=%s", payload.session_id)
        return _fallback_chat_response(payload.user_id, session_id=payload.session_id)


@router.get("/jobs/status", response_model=ChatResponse)
def job_status(
    user_id: str,
    conversation_id: str,
    session_id: str = None,
    service: JobService = Depends(get_job_service),
):
    """The FE spinner polls this every ~30s to advance a running diagnostic job."""
    try:
        return service.advance_job(user_id, conversation_id, session_id)
    except (NotFound, UpstreamUnavailable):
        # UpstreamUnavailable means the diagnostics/troubleshoot app is unreachable or
        # unconfigured -- a 502 says that, where a blanket 500 would blame us.
        raise
    except Exception:
        logger.exception("job_status failed conv_id=%s", conversation_id)
        raise HTTPException(status_code=500, detail="Failed to read job status")


@router.get("/sessions/{user_id}", response_model=SessionListResponse)
def get_sessions(
    user_id: str,
    service: HistoryService = Depends(get_history_service),
):
    """List a user's most recent sessions (up to 20), newest first."""
    try:
        return service.list_sessions(user_id)
    except Exception:
        logger.exception("get_sessions failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load sessions")


@router.get(
    "/conversations/{user_id}/{session_id}", response_model=TranscriptResponse
)
def get_conversation(
    user_id: str,
    session_id: str,
    service: HistoryService = Depends(get_history_service),
):
    """Return the session's messages as a flat list of {role, content}."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    try:
        return service.get_transcript(user_id, session_id)
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")
