import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import openai
import requests
from azure.ai.projects import AIProjectClient
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

# Multilingual agent (its own Function App): detects the user's language and
# translates outgoing messages into it. It's a STATELESS utility -- it runs on
# the Agents API (threads/runs) and creates its OWN thread per request, so we do
# NOT pass a conversation id and it never touches our Foundry conversation.
# Expected contract (structured JSON body):
#   detect    -> {"action": "detect", "text": "<user msg>"}
#                returns {"code": "fr", "supported": true, "confidence": 0.97, ...}
#   translate -> {"action": "translate", "target": "fr", "text": "<english>"}
#                returns {"translated": "<text in target language>"}
MULTILINGUAL_AGENT = os.getenv("MULTILINGUAL_AGENT_URL", "")
# Language assumed when detection is unavailable/uncertain/unsupported. Outgoing
# messages are NOT translated when the detected language equals DEFAULT_LANG.
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")
# Below this detection confidence we keep the current language (avoids a short
# reply like "ok" wrongly flipping the conversation's language).
MIN_DETECT_CONFIDENCE = float(os.getenv("MIN_DETECT_CONFIDENCE", "0.55"))

# Field in an agent's JSON response holding the human-readable text (used only
# for agents whose reply is shown to the user). Structured agents (orchestrator/
# outlook) are read as the FULL dict via run_agent_json().
AGENT_RESPONSE_FIELD = os.getenv("AGENT_RESPONSE_FIELD", "message")
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
    """Lazily initialize the Foundry client once per worker process.

    Azure Functions does not run FastAPI's lifespan, so client setup happens on
    the first request and is cached in the module-level `state` dict. (Azure SQL
    connections are opened per-request via get_conn(), not cached here.)
    """
    global _state_ready
    if _state_ready:
        return state

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
    state["project_client"] = project_client

    _state_ready = True
    logger.info("State initialized (lazy)")
    return state


# ---------------------------------------------------------------------------
# Azure SQL helpers
# ---------------------------------------------------------------------------
# If SQLITE_DB_PATH is set, use a LOCAL SQLite file instead of Azure SQL.
# Handy for testing on a machine that can't reach Azure SQL. Leave it empty to
# use the real Azure SQL connection (SQL_CONNECTION_STRING).
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "")

# Schema for SQLite mode. On a Function App nobody runs setup_sqlite.py, so we
# create the tables on first connect (idempotent) to avoid "no such table".
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT NOT NULL, user_id TEXT NOT NULL, conversation_id TEXT, stage TEXT,
    vars TEXT, question TEXT, answer TEXT, session_id TEXT, seq INTEGER,
    previous_conversation_id TEXT, summary TEXT, title TEXT, rolled_over INTEGER,
    created_at TEXT, updated_at TEXT, PRIMARY KEY (user_id, id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT NOT NULL, session_id TEXT, user_id TEXT NOT NULL,
    current_conversation_id TEXT, title TEXT, created_at TEXT, updated_at TEXT,
    PRIMARY KEY (user_id, id)
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
    row = cur.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def _fetchall_dicts(cur) -> list:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


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


def call_agent(conv_id: str, agent_url: str, message: str) -> dict:
    """POST to an agent's Function App and return its FULL JSON response dict.

    Each agent is a separate Function App. We send the conversation id (for
    continuity) and the message; the agent runs internally and returns JSON.
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
        return resp.json()
    except requests.RequestException as exc:
        logger.error("agent call failed url=%s: %s", agent_url, exc)
        raise HTTPException(status_code=502, detail="Agent call failed")


def run_agent_json(conv_id: str, agent_url: str, message: str) -> dict:
    """Return the agent's FULL response dict (for structured agents)."""
    return call_agent(conv_id, agent_url, message)


def run_agent(conv_id: str, agent_url: str, message: str) -> str:
    """Return just the human-readable text (AGENT_RESPONSE_FIELD, default
    'message') from the agent's response, for messages shown to the user."""
    return call_agent(conv_id, agent_url, message).get(AGENT_RESPONSE_FIELD, "")


def _post_multilingual(payload: dict) -> dict:
    """POST a structured request to the multilingual Function App, return its JSON.

    Uses its own request shape (not call_agent's {conversation_id, message}) because
    the multilingual agent takes {action, text, target} and is stateless -- no
    conversation id, no shared thread. Raises on transport error; callers catch.
    """
    resp = requests.post(MULTILINGUAL_AGENT, json=payload, timeout=AGENT_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def detect_language(text: str, fallback: str = "") -> str:
    """Detect the user's language (ISO code) via the multilingual agent, every turn.

    Keeps `fallback` (the previously detected language) -- or DEFAULT_LANG if none --
    when the agent isn't configured, the text is empty, the language is unsupported,
    detection is low-confidence, or the call fails. So a short reply like "ok" never
    breaks the chat or wrongly flips the conversation language.
    """
    fb = fallback or DEFAULT_LANG
    if not MULTILINGUAL_AGENT or not text or not text.strip():
        return fb
    try:
        resp = _post_multilingual({"action": "detect", "text": text})
        if not resp.get("supported"):
            return fb  # unsupported language -> stay in the default language
        if float(resp.get("confidence", 1)) < MIN_DETECT_CONFIDENCE:
            return fb  # too uncertain -> keep the current language
        return (resp.get("code") or fb).strip()
    except Exception:
        logger.exception("detect_language failed")
        return fb


def translate_messages(messages: list, lang: str) -> list:
    """Translate each outgoing message into `lang` (ISO code) via the multilingual
    agent, right before it goes to the frontend.

    Per-message so the FE can still render each as its own bubble. No-ops (returns
    the originals) when the agent isn't configured or lang == DEFAULT_LANG. A
    per-message failure keeps that message's original text, so a translation outage
    degrades gracefully instead of 502-ing the whole chat.
    """
    if not MULTILINGUAL_AGENT or not lang or lang == DEFAULT_LANG:
        return messages
    out = []
    for m in messages:
        if not m or not m.strip():
            out.append(m)
            continue
        try:
            resp = _post_multilingual(
                {"action": "translate", "target": lang, "text": m}
            )
            out.append(resp.get("translated") or m)
        except Exception:
            logger.exception("translate failed")
            out.append(m)
    return out


def translate_to_english(text: str, lang: str) -> str:
    """Translate an inbound user message into English (DEFAULT_LANG).

    Lets the flow logic (the "yes"/"no" checks) and the sub-agents all operate in
    one language regardless of what the user typed. No-op when the multilingual
    agent isn't configured, the text is empty, or the user is already writing in
    DEFAULT_LANG. Returns the original text on failure so a translation outage
    never blocks the conversation.
    """
    if (
        not MULTILINGUAL_AGENT
        or not text
        or not text.strip()
        or not lang
        or lang == DEFAULT_LANG
    ):
        return text
    try:
        resp = _post_multilingual(
            {"action": "translate", "lang": DEFAULT_LANG, "message": text}
        )
        return resp.get("reply") or text
    except Exception:
        logger.exception("translate_to_english failed")
        return text


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
    """Advance the support flow by one turn based on the current stage.

    Detects and stores the user's language, then dispatches to the handler for the
    current stage. Returns (messages, new_stage, vars). Raises 409 if the stage is
    already terminal (unknown/DONE).
    """
    messages: list[str] = []
    # Re-detect every turn so a mid-conversation language switch is honored; keep
    # the previously stored language when detection is empty/uncertain/unsupported.
    vars["lang"] = detect_language(user_message, fallback=vars.get("lang", ""))

    # Work internally in English: translate the inbound message so the yes/no
    # checks AND the sub-agents all see English, regardless of the user's language.
    # (The endpoints still store request.message -- the original native text.)
    user_message = translate_to_english(user_message, vars["lang"])

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
    """First turn: classify the issue via the orchestrator agent and route it.

    Ends the flow for non-IT issues, hands non-Outlook IT issues to ServiceNow,
    and otherwise continues into the Outlook flow.
    """
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
    """Outlook-issue turn: query the Outlook agent and gather its reply.

    Stays in AWAITING_OUTLOOK until the agent signals handoff; then delegates to
    _post_handoff to branch into guidance or the diagnostics/ticketing path.
    """
    outlook = run_agent_json(conv_id, OUTLOOK_AGENT, user_message)
    vars["outlook"] = outlook

    if outlook.get("message"):
        messages.append(outlook["message"])

    if not outlook.get("handoff"):
        return messages, AWAITING_OUTLOOK, vars

    return _post_handoff(conv_id, outlook, vars, messages)


def _post_handoff(conv_id, outlook, vars, messages):
    """After Outlook handoff, pick the next path.

    If the agent gave self-service guidance, ask whether it resolved the issue
    (AWAITING_RESOLVED). Otherwise raise a ServiceNow incident, run diagnostics,
    and ask to proceed with troubleshooting (AWAITING_PROCEED).
    """
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
    """Handle the 'did the guidance resolve it?' answer.

    'yes' closes the conversation; anything else falls back to ServiceNow. Flow
    ends (DONE) either way.
    """
    if "yes" in user_message.lower():
        messages.append("Glad it worked")
        return messages, DONE, vars
    messages.append(run_agent(conv_id, SERVICENOW_AGENT, ""))
    return messages, DONE, vars


def _proceed_turn(conv_id, user_message, vars, messages):
    """Handle the 'proceed with troubleshooting?' answer.

    'yes' runs the Troubleshoot agent and asks whether the issue is resolved
    (AWAITING_FINAL); anything else falls back to ServiceNow and ends (DONE).
    """
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
    """Handle the final 'issue resolved?' answer after troubleshooting.

    'yes' resolves the ServiceNow incident; anything else falls back to ServiceNow
    (e.g. escalate/keep open). Flow ends (DONE) either way.
    """
    if "yes" in user_message.lower():
        messages.append(run_agent(conv_id, SERVICENOW_AGENT, "action=resolve"))
        return messages, DONE, vars
    messages.append(run_agent(conv_id, SERVICENOW_AGENT, ""))
    return messages, DONE, vars


# ---------------------------------------------------------------------------
# Persistence: conversations + sessions tables (Azure SQL)
# ---------------------------------------------------------------------------
def load_state(conn, user_id: str, conv_id: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, conversation_id, stage, vars, question, answer, "
        "session_id, seq, previous_conversation_id, summary, title, rolled_over, "
        "created_at, updated_at FROM conversations WHERE user_id = ? AND id = ?",
        (user_id, conv_id),
    )
    row = _fetchone_dict(cur)
    if row is None:
        return None
    row["vars"] = json.loads(row["vars"]) if row.get("vars") else {}
    return row


def upsert_conversation(conn, item: dict) -> None:
    vars_val = item.get("vars")
    vars_json = (
        json.dumps(vars_val, default=str)
        if isinstance(vars_val, (dict, list))
        else vars_val
    )
    rolled_over = 1 if item.get("rolled_over") else 0

    cur = conn.cursor()
    cur.execute(
        "UPDATE conversations SET conversation_id = ?, stage = ?, vars = ?, "
        "question = ?, answer = ?, session_id = ?, seq = ?, "
        "previous_conversation_id = ?, summary = ?, title = ?, rolled_over = ?, "
        "created_at = ?, updated_at = ? WHERE user_id = ? AND id = ?",
        (
            item.get("conversation_id"),
            item.get("stage"),
            vars_json,
            item.get("question"),
            item.get("answer"),
            item.get("session_id"),
            item.get("seq"),
            item.get("previous_conversation_id"),
            item.get("summary"),
            item.get("title"),
            rolled_over,
            item.get("created_at"),
            item.get("updated_at"),
            item.get("user_id"),
            item.get("id"),
        ),
    )
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO conversations (id, user_id, conversation_id, stage, vars, "
            "question, answer, session_id, seq, previous_conversation_id, summary, "
            "title, rolled_over, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                item.get("previous_conversation_id"),
                item.get("summary"),
                item.get("title"),
                rolled_over,
                item.get("created_at"),
                item.get("updated_at"),
            ),
        )
    conn.commit()


def save_state(
    conn,
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
    existing = load_state(conn, user_id, conv_id) or {}
    item = {
        "id": conv_id,
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
        "rolled_over": existing.get("rolled_over"),
    }
    if not existing:
        item["title"] = question
        item["created_at"] = now
    else:
        item["title"] = existing.get("title")
        item["created_at"] = existing.get("created_at", now)
    upsert_conversation(conn, item)


def load_session(conn, user_id: str, session_id: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, session_id, user_id, current_conversation_id, title, "
        "created_at, updated_at FROM sessions WHERE user_id = ? AND id = ?",
        (user_id, session_id),
    )
    return _fetchone_dict(cur)


def upsert_session(conn, item: dict) -> None:
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


@app.get("/")
def read_root():
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


@app.post("/chat")
def chat(request: ChatRequest):
    _ensure_state()
    conn = get_conn()
    try:
        session_id = "sess_" + uuid.uuid4().hex
        conversation = create_conversation()
        messages, stage, vars = step(conversation.id, request.message, None, {})
        messages = translate_messages(messages, vars.get("lang"))
        answer = "\n\n".join(m for m in messages if m)
        save_state(
            conn,
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


@app.post("/chat/continue")
def continue_chat(request: ContinueChatRequest):
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
        conversation = load_state(conn, request.user_id, conv_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        stage = conversation.get("stage", DONE)
        vars = conversation.get("vars", {})

        if stage == DONE:
            # --- ROLLOVER: cumulative summary -> new seeded conversation ---
            prior_summary = vars.get("carried_summary", "")
            summary = summarize_conversation(conv_id, vars, prior_summary=prior_summary)

            conversation["summary"] = summary
            conversation["rolled_over"] = True
            upsert_conversation(conn, conversation)

            new_conv = create_conversation(seed_summary=summary)
            new_vars = {"carried_summary": summary} if summary else {}
            messages, new_stage, new_vars = step(
                new_conv.id, request.message, None, new_vars
            )
            messages = translate_messages(messages, new_vars.get("lang"))
            answer = "\n\n".join(m for m in messages if m)

            save_state(
                conn,
                request.user_id,
                new_conv.id,
                stage=new_stage,
                vars=new_vars,
                question=request.message,
                answer=answer,
                session_id=session_id,
                seq=conversation.get("seq", 0) + 1,
                previous_conversation_id=conv_id,
            )
            save_session(
                conn,
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
            messages = translate_messages(messages, new_vars.get("lang"))
            answer = "\n\n".join(m for m in messages if m)
            save_state(
                conn,
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


@app.get("/sessions/{user_id}")
def get_sessions(user_id: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        if SQLITE_DB_PATH:
            cur.execute(
                "SELECT id, session_id, current_conversation_id, title, "
                "created_at, updated_at FROM sessions WHERE user_id = ? "
                "ORDER BY updated_at DESC LIMIT 20",
                (user_id,),
            )
        else:
            cur.execute(
                "SELECT TOP 20 id, session_id, current_conversation_id, title, "
                "created_at, updated_at FROM sessions WHERE user_id = ? "
                "ORDER BY updated_at DESC",
                (user_id,),
            )
        rows = _fetchall_dicts(cur)
        return {"sessions": rows}
    except Exception:
        logger.exception("get_sessions failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load sessions")
    finally:
        conn.close()


def _conversation_messages(openai_client, conversation_id: str) -> list:
    """Fetch the visible message turns for one Foundry conversation."""
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
            continue  # hide seeded prior-session context
        result.append({"role": str(item.role), "content": text})
    return result


@app.get("/conversations/{user_id}")
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
        cols = (
            "id, conversation_id, stage, question, answer, session_id, seq, "
            "title, created_at, updated_at"
        )
        # Step 1: get the conversation rows (conv_ids) from SQL
        if session_id:
            cur.execute(
                f"SELECT {cols} FROM conversations WHERE user_id = ? AND session_id = ? "
                "ORDER BY seq ASC",
                (user_id, session_id),
            )
        elif SQLITE_DB_PATH:
            cur.execute(
                f"SELECT {cols} FROM conversations WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT 10",
                (user_id,),
            )
        else:
            cur.execute(
                f"SELECT TOP 10 {cols} FROM conversations "
                "WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        rows = _fetchall_dicts(cur)
    except Exception:
        logger.exception("get_conversation failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="Failed to load conversations")
    finally:
        conn.close()

    # Step 2: attach each conversation's messages from Foundry
    for r in rows:
        try:
            r["messages"] = _conversation_messages(openai_client, r["conversation_id"])
            r["messages_ok"] = True
        except Exception:
            logger.exception("load messages failed conv_id=%s", r.get("conversation_id"))
            r["messages"] = []
            r["messages_ok"] = False  # FE can flag "couldn't load this part"

    return {"user_id": user_id, "session_id": session_id, "conversations": rows}
