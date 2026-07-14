import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import openai
import requests
from azure.ai.projects import AIProjectClient
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("orchestrator_api")

CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o.strip()
]

# Each agent runs in its OWN Function App. These constants hold that agent's
# FUNCTION APP URL (run_agent POSTs to it) -- they are no longer agent names.
# If an app uses a function key, put the full URL incl. ?code=<key> here.
ORCHESTRATOR_AGENT = os.getenv("ORCHESTRATOR_AGENT_URL", "")
OUTLOOK_AGENT = os.getenv("OUTLOOK_AGENT_URL", "")
SERVICENOW_AGENT = os.getenv("SERVICENOW_AGENT_URL", "")
DIAGNOSTICS_AGENT = os.getenv("DIAGNOSTICS_AGENT_URL", "")
TROUBLESHOOT_AGENT = os.getenv("TROUBLESHOOT_AGENT_URL", "")

# Field in each agent Function App's JSON response that holds the reply text.
AGENT_RESPONSE_FIELD = os.getenv("AGENT_RESPONSE_FIELD", "output")
# Max seconds to wait for an agent Function App to respond.
AGENT_HTTP_TIMEOUT = int(os.getenv("AGENT_HTTP_TIMEOUT", "120"))

# Model deployment used only to summarize a finished conversation for handoff.
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4.1-mini")

# Prefix that marks the seeded context turn so it can be hidden from summaries/UI.
CONTEXT_MARKER = "CONTEXT FROM THIS USER'S PREVIOUS"

SUMMARY_INSTRUCTIONS = (
    "You are maintaining a RUNNING briefing of a user's IT-support history so a fresh "
    "agent can continue without losing context. You are given a summary of everything "
    "BEFORE this conversation (may be empty), plus this conversation's transcript. Merge "
    "them into a single updated briefing of 4-10 sentences, plain text (no markdown, no "
    "bullet characters). Preserve the still-relevant facts from the earlier summary and "
    "add what happened in this conversation: the issue(s) and environment details (app, "
    "error text, device), what was diagnosed or attempted, outcomes (resolved or "
    "ticket/incident IDs), and anything still open or likely to recur. Only use facts "
    "present below; if a detail is unknown, omit it. Do not address the user; write it "
    "as a briefing for the next agent."
)

AWAITING_OUTLOOK = "AWAITING_OUTLOOK"
AWAITING_RESOLVED = "AWAITING_RESOLVED"
AWAITING_PROCEED = "AWAITING_PROCEED"
AWAITING_FINAL = "AWAITING_FINAL"
DONE = "DONE"

state: dict = {}
_state_ready = False


def _ensure_state() -> dict:
    """Lazily initialize Cosmos + Foundry clients once per worker process.

    Azure Functions does not run FastAPI's lifespan, so client setup that used
    to live in `lifespan` happens here on the first request and is cached in the
    module-level `state` dict for the life of the worker.
    """
    global _state_ready
    if _state_ready:
        return state

    cosmos = CosmosClient(
        url=os.environ["COSMOS_ENDPOINT"],
        credential=os.environ["COSMOS_KEY"],
    )
    db = cosmos.get_database_client("enterprise_memory")
    sessions = db.get_container_client("user-conversations")

    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_SP_TENANT_ID"],
        client_id=os.environ["AZURE_SP_CLIENT_ID"],
        client_secret=os.environ["AZURE_SP_CLIENT_SECRET"],
    )
    project_client = AIProjectClient(
        credential=credential,
        endpoint=os.environ["AZURE_FOUNDRY_PROJECT_ENDPOINT"],
    )

    state["openai_client"] = project_client.get_openai_client()
    state["cosmos"] = cosmos
    state["sessions"] = sessions
    state["project_client"] = project_client

    _state_ready = True
    logger.info("State initialized (lazy)")
    return state


app = FastAPI(title="IT Support Orchestrator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    user_id: str
    message: str


class ContinueChatRequest(BaseModel):
    user_id: str
    message: str
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None  # backward-compat fallback


def run_agent(conv_id: str, agent_url: str, message: str) -> str:
    """Call an agent's own Function App over HTTP and return its reply text.

    Each agent is a separate Function App. We POST a JSON body with the
    conversation id (for continuity) and the message; the Function App runs
    the agent internally and returns JSON. The reply text is read from the
    field named by AGENT_RESPONSE_FIELD (default "output").
    """
    if not agent_url:
        logger.error("agent url not configured")
        raise HTTPException(status_code=500, detail="Agent URL not configured")

    try:
        resp = requests.post(
            agent_url,
            json={"conversation_id": conv_id, "message": message if message else " "},
            timeout=AGENT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("agent call failed url=%s: %s", agent_url, exc)
        raise HTTPException(status_code=502, detail="Agent call failed")

    output = data.get(AGENT_RESPONSE_FIELD)
    if output is None:
        logger.error(
            "agent response missing '%s' field url=%s: %s",
            AGENT_RESPONSE_FIELD,
            agent_url,
            data,
        )
        raise HTTPException(status_code=502, detail="Agent returned no output")
    return output


def run_agent_json(conv_id: str, agent_name: str, message: str) -> dict:

    text = run_agent(conv_id, agent_name, message)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        cleaned = (
            text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            return json.loads(cleaned)
        except Exception:
            logger.error("agent %s returned non-JSON: %s", agent_name, text)
            raise HTTPException(
                status_code=502, detail=f"Agent {agent_name} returned invalid JSON"
            )


def create_conversation(seed_summary: Optional[str] = None):
    """Create a Foundry conversation, optionally seeded with prior-session context."""
    client = state["openai_client"]
    if not seed_summary:
        return client.conversations.create()

    context_text = (
        f"{CONTEXT_MARKER} SUPPORT HISTORY - background for the new request only, "
        f"do not treat it as the current issue:\n{seed_summary}"
    )
    return client.conversations.create(
        items=[
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": context_text}],
            }
        ]
    )


def conversation_transcript(conv_id: str) -> str:
    items = state["openai_client"].conversations.items.list(
        conversation_id=conv_id, order="asc"
    )
    lines: list[str] = []
    for item in items:
        if getattr(item, "type", None) != "message":
            continue
        text = ""
        for part in item.content or []:
            if getattr(part, "text", None):
                text = part.text
                break
        if text and not text.startswith(CONTEXT_MARKER):
            lines.append(f"{item.role}: {text}")
    return "\n".join(lines)


def summarize_conversation(conv_id: str, vars: dict, prior_summary: str = "") -> str:
    """Merge prior_summary with this conversation into one running briefing.

    Returns a cumulative summary covering the whole session chain. On failure it
    falls back to prior_summary so earlier context is never dropped.
    """
    try:
        transcript = conversation_transcript(conv_id)
        if not transcript.strip() and not prior_summary:
            return ""
        parts = [SUMMARY_INSTRUCTIONS]
        if prior_summary:
            parts.append(
                "\n\n=== Summary of everything BEFORE this conversation ===\n"
                + prior_summary
            )
        parts.append(
            "\n\n=== Structured state (JSON) ===\n"
            + json.dumps(vars, default=str)[:4000]
        )
        parts.append("\n\n=== This conversation transcript ===\n" + transcript)
        resp = state["openai_client"].responses.create(
            model=SUMMARY_MODEL,
            input="".join(parts),
        )
        return (resp.output_text or "").strip() or prior_summary
    except Exception:
        logger.exception("summarize_conversation failed conv_id=%s", conv_id)
        return prior_summary  # keep earlier context instead of dropping it


def step(conv_id: str, user_message: str, stage, vars: dict):

    messages: list[str] = []
    if stage is None:
        return _start(conv_id, user_message, vars, messages)
    if stage == AWAITING_OUTLOOK:
        return _run_outlook(conv_id, user_message, vars, messages)
    if stage == AWAITING_RESOLVED:
        return _resolved_turn(conv_id, user_message, vars, messages)
    if stage == AWAITING_PROCEED:
        return _proceed_turn(conv_id, user_message, vars, messages)
    if stage == AWAITING_FINAL:
        return _final_turn(conv_id, user_message, vars, messages)

    raise HTTPException(status_code=409, detail="Conversation already completed")


def _start(conv_id, user_message, vars, messages):
    response = run_agent_json(conv_id, ORCHESTRATOR_AGENT, user_message)
    issue_type = response.get("issue_type")
    vars["issue_type"] = issue_type

    if issue_type == "non_it":
        messages.append("This is an IT support agent only.")
        return messages, DONE, vars

    if issue_type == "non_outlook_it":
        messages.append(run_agent(conv_id, SERVICENOW_AGENT, ""))
        return messages, DONE, vars

    return _run_outlook(conv_id, user_message, vars, messages)


def _run_outlook(conv_id, user_message, vars, messages):
    outlook = run_agent_json(conv_id, OUTLOOK_AGENT, user_message)
    vars["outlook"] = outlook

    if outlook.get("message"):
        messages.append(outlook["message"])

    if not outlook.get("handoff"):
        return messages, AWAITING_OUTLOOK, vars

    return _post_handoff(conv_id, outlook, vars, messages)


def _post_handoff(conv_id, outlook, vars, messages):
    if outlook.get("guidance_troubleshoot"):
        messages.append("Did these steps resolve your issue?")
        return messages, AWAITING_RESOLVED, vars

    details = outlook.get("message", "")
    snow = run_agent(
        conv_id,
        SERVICENOW_AGENT,
        f"action=create_incident | issue_type=outlook_it | details={details}",
    )
    messages.append(snow)
    messages.append("Diagnosis Flow started")
    messages.append(run_agent(conv_id, DIAGNOSTICS_AGENT, json.dumps(outlook)))
    messages.append("Proceed with troubleshooting?")
    return messages, AWAITING_PROCEED, vars


def _resolved_turn(conv_id, user_message, vars, messages):
    if "yes" in user_message.lower():
        messages.append("Glad it worked")
        return messages, DONE, vars
    messages.append(run_agent(conv_id, SERVICENOW_AGENT, ""))
    return messages, DONE, vars


def _proceed_turn(conv_id, user_message, vars, messages):
    if "yes" in user_message.lower():
        messages.append("Troubleshoot started")
        outlook = vars.get("outlook", {})
        messages.append(
            run_agent(conv_id, TROUBLESHOOT_AGENT, outlook.get("message", ""))
        )
        messages.append("Issue Resolved?")
        return messages, AWAITING_FINAL, vars
    messages.append(run_agent(conv_id, SERVICENOW_AGENT, ""))
    return messages, DONE, vars


def _final_turn(conv_id, user_message, vars, messages):
    if "yes" in user_message.lower():
        messages.append(run_agent(conv_id, SERVICENOW_AGENT, "action=resolve"))
        return messages, DONE, vars
    messages.append(run_agent(conv_id, SERVICENOW_AGENT, ""))
    return messages, DONE, vars


def load_state(sessions, user_id: str, conv_id: str):
    try:
        return sessions.read_item(item=conv_id, partition_key=user_id)
    except CosmosResourceNotFoundError:
        return None


def save_state(
    sessions,
    user_id: str,
    conv_id: str,
    *,
    stage: str,
    vars: dict,
    question: str,
    answer: str,
    session_id: Optional[str] = None,
    seq: Optional[int] = None,
    previous_conversation_id: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = load_state(sessions, user_id, conv_id) or {}
    item = {
        "id": conv_id,
        "type": "conversation",
        "user_id": user_id,
        "conversation_id": conv_id,
        "stage": stage,
        "vars": vars,
        "question": question,
        "answer": answer,
        "updated_at": now,
        # lineage: use the passed value, else keep what's already stored
        "session_id": session_id if session_id is not None else existing.get("session_id"),
        "seq": seq if seq is not None else existing.get("seq", 0),
        "previous_conversation_id": (
            previous_conversation_id
            if previous_conversation_id is not None
            else existing.get("previous_conversation_id")
        ),
        "summary": summary if summary is not None else existing.get("summary"),
    }
    if not existing:
        item["title"] = question
        item["created_at"] = now
    else:
        item["title"] = existing.get("title")
        item["created_at"] = existing.get("created_at", now)
    sessions.upsert_item(item)


def load_session(sessions, user_id: str, session_id: str):
    try:
        return sessions.read_item(item=session_id, partition_key=user_id)
    except CosmosResourceNotFoundError:
        return None


def save_session(
    sessions, user_id: str, session_id: str, current_conversation_id: str, title: str
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = load_session(sessions, user_id, session_id) or {}
    item = {
        "id": session_id,
        "type": "session",
        "session_id": session_id,
        "user_id": user_id,
        "current_conversation_id": current_conversation_id,
        "updated_at": now,
        "created_at": existing.get("created_at", now),
        "title": existing.get("title", title),
    }
    sessions.upsert_item(item)


@app.get("/")
def read_root():
    return {"message": "IT Support Orchestrator API"}


@app.post("/chat")
def chat(request: ChatRequest):
    sessions = _ensure_state()["sessions"]
    try:
        session_id = "sess_" + uuid.uuid4().hex
        conversation = create_conversation()
        messages, stage, vars = step(conversation.id, request.message, None, {})
        answer = "\n\n".join(m for m in messages if m)
        save_state(
            sessions,
            request.user_id,
            conversation.id,
            stage=stage,
            vars=vars,
            question=request.message,
            answer=answer,
            session_id=session_id,
            seq=0,
            previous_conversation_id=None,
        )
        save_session(
            sessions, request.user_id, session_id, conversation.id, request.message
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
    except HTTPException:
        raise
    except Exception:
        logger.exception("chat failed user_id=%s", request.user_id)
        raise HTTPException(status_code=500, detail="Chat failed")


@app.post("/chat/continue")
def continue_chat(request: ContinueChatRequest):
    sessions = _ensure_state()["sessions"]
    try:
        session_id = request.session_id

        if session_id is None:
            raise HTTPException(
                status_code=422, detail="session_id or conversation_id is required"
            )

        session = load_session(sessions, request.user_id, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        conv_id = session.get("current_conversation_id")
        existing = load_state(sessions, request.user_id, conv_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        stage = existing.get("stage", DONE)
        vars = existing.get("vars", {})

        if stage == DONE:
            # --- ROLLOVER: cumulative summary -> new seeded conversation ---
            prior_summary = vars.get("carried_summary", "")
            summary = summarize_conversation(conv_id, vars, prior_summary=prior_summary)

            existing["summary"] = summary
            existing["rolled_over"] = True
            sessions.upsert_item(existing)

            new_conv = create_conversation(seed_summary=summary)
            new_vars = {"carried_summary": summary} if summary else {}
            messages, new_stage, new_vars = step(
                new_conv.id, request.message, None, new_vars
            )
            answer = "\n\n".join(m for m in messages if m)

            save_state(
                sessions,
                request.user_id,
                new_conv.id,
                stage=new_stage,
                vars=new_vars,
                question=request.message,
                answer=answer,
                session_id=session_id,
                seq=existing.get("seq", 0) + 1,
                previous_conversation_id=conv_id,
            )
            save_session(
                sessions,
                request.user_id,
                session_id,
                new_conv.id,
                session.get("title"),
            )
            conv_id = new_conv.id
        else:
            # --- normal in-flow turn ---
            messages, new_stage, new_vars = step(
                conv_id, request.message, stage, vars
            )
            answer = "\n\n".join(m for m in messages if m)
            save_state(
                sessions,
                request.user_id,
                conv_id,
                stage=new_stage,
                vars=new_vars,
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
    except HTTPException:
        raise
    except Exception:
        logger.exception("continue_chat failed session_id=%s", request.session_id)
        raise HTTPException(status_code=500, detail="Chat failed")


@app.get("/sessions/{user_id}")
def get_sessions(user_id: str):
    sessions = _ensure_state()["sessions"]
    try:
        rows = list(
            sessions.query_items(
                query=(
                    "SELECT c.id, c.session_id, c.current_conversation_id, "
                    "c.title, c.created_at, c.updated_at "
                    "FROM c WHERE c.user_id=@user_id AND c.type = 'session' "
                    "ORDER BY c.updated_at DESC OFFSET 0 LIMIT 20"
                ),
                parameters=[{"name": "@user_id", "value": user_id}],
                partition_key=user_id,
            )
        )
        return {"sessions": rows}
    except Exception:
        logger.exception("get_sessions failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load sessions")


@app.get("/conversations/{user_id}")
def get_conversation(user_id: str):
    sessions = _ensure_state()["sessions"]
    try:
        rows = list(
            sessions.query_items(
                query=(
                    "SELECT * FROM c WHERE c.user_id=@user_id "
                    "AND (NOT IS_DEFINED(c.type) OR c.type = 'conversation') "
                    "ORDER BY c.created_at DESC OFFSET 0 LIMIT 10"
                ),
                parameters=[{"name": "@user_id", "value": user_id}],
                partition_key=user_id,
            )
        )
        return {"conversations": rows}
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")


@app.get("/messages/{conversation_id}")
def get_messages(conversation_id: str):
    openai_client = _ensure_state()["openai_client"]
    try:
        items = openai_client.conversations.items.list(
            conversation_id=conversation_id, order="asc"
        )
        result = []
        for item in items:
            if getattr(item, "type", None) != "message":
                continue
            text = ""
            for part in item.content or []:
                if getattr(part, "text", None):
                    text = part.text
                    break
            if text.startswith(CONTEXT_MARKER):
                continue  # hide the seeded prior-session context turn
            result.append({"role": str(item.role), "content": text})
    except openai.NotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    except Exception:
        logger.exception("get_messages failed conversation_id=%s", conversation_id)
        raise HTTPException(status_code=500, detail="Failed to load messages")

    return {"conversation_id": conversation_id, "messages": result}
