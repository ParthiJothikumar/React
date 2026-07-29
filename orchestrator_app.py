import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
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
# Two-stage classification (each its own Function App):
#   FIRST  -> classifies the issue and asks follow-ups. Response:
#             follow-up: {"chat_close": false, "kb_id": null, "summary": null,
#                         "agent_message": "<next question>"}
#             done:      {"chat_close": true, "kb_id": "kb100",
#                         "summary": "...", "agent_message": ""}
#   SECOND -> given {summary, kb_id}, decides how to resolve. Response:
#             {"mode": "manual" | "automate", "steps": "...", "agent_message": "..."}
FIRST_CLASSIFICATION_AGENT = os.getenv("FIRST_CLASSIFICATION_AGENT_URL", "")
SECOND_CLASSIFICATION_AGENT = os.getenv("SECOND_CLASSIFICATION_AGENT_URL", "")
SERVICENOW_AGENT = os.getenv("SERVICENOW_AGENT_URL", "")
DIAGNOSTICS_AGENT = os.getenv("DIAGNOSTICS_AGENT_URL", "")
TROUBLESHOOT_AGENT = os.getenv("TROUBLESHOOT_AGENT_URL", "")

# Multilingual agent (its own Function App): detects the user's language and
# translates outgoing messages into it. It's a STATELESS utility -- it runs on
# the Agents API (threads/runs) and creates its OWN thread per request, so we do
# NOT pass a conversation id and it never touches our Foundry conversation.
# Expected contract (structured JSON body):
#   detect    -> {"agent": "detect", "message": "<text>"}
#                returns {"code": "fr", "supported": true, ...}
#   translate -> {"agent": "translate", "lang": "<target ISO>", "message": "<text>"}
#                returns {"reply": "<text in target language>"}
MULTILINGUAL_AGENT = os.getenv("MULTILINGUAL_AGENT_URL", "")
# Language assumed when detection is unavailable/uncertain/unsupported. Outgoing
# messages are NOT translated when the detected language equals DEFAULT_LANG.
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")

# Max seconds to wait for an agent Function App to respond.
AGENT_HTTP_TIMEOUT = int(os.getenv("AGENT_HTTP_TIMEOUT", "120"))

AWAITING_ISSUE = "AWAITING_ISSUE"
AWAITING_CLASSIFY = "AWAITING_CLASSIFY"
AWAITING_RESOLVED = "AWAITING_RESOLVED"
AWAITING_PROCEED = "AWAITING_PROCEED"
AWAITING_FINAL = "AWAITING_FINAL"
DONE = "DONE"

# Non-actionable messages (greeting or general/non-IT): the orchestrator re-prompts
# for an Outlook issue up to MAX_NONACTIONABLE times (shared counter), then ends the
# conversation. No incident is created for these; the interaction (opened at the
# start of every conversation) is closed when the conversation ends.
MAX_NONACTIONABLE = int(os.getenv("MAX_NONACTIONABLE", "2"))
GREETING_PROMPT = "Hi! How can I help you with your Outlook issue?"
NON_IT_PROMPT = "This is Outlook IT support -- what Outlook issue can I help you with?"
NONACTIONABLE_END_MESSAGE = (
    "Thanks for contacting. For any Outlook support, please start a new conversation."
)

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

    credential = DefaultAzureCredential()
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
    title TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY (user_id, id)
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
    conversation_id: Optional[str] = None 


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
        #resp.raise_for_status() is the line that looks at the status code and, if it's 4xx/5xx status, raises requests.HTTPError
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.error("agent call failed url=%s: %s", agent_url, exc)
        raise HTTPException(status_code=502, detail="Agent call failed")


def run_agent_json(conv_id: str, agent_url: str, message: str) -> dict:
    """Return the agent's FULL response dict (for structured agents)."""
    return call_agent(conv_id, agent_url, message)


def run_agent(conv_id: str, agent_url: str, message: str) -> str:
    """Return just the human-readable text from the agent's response.

    Reads the first present of 'message' / 'reply' / 'agent_message' (empty string
    if none), for replies shown to the user. Structured agents use run_agent_json.
    """
    resp = call_agent(conv_id, agent_url, message)
    return resp.get("message") or resp.get("reply") or resp.get("agent_message") or ""


def call_second_classification(conv_id: str, summary: str, kb_id) -> dict:
    """Ask the SECOND classification agent how to resolve the issue.

    Posts the first agent's {kb_id, summary} and returns its decision dict
    {mode, steps, agent_message}. Raises HTTPException on failure (the endpoint
    catches it and returns the fallback message).
    """
    if not SECOND_CLASSIFICATION_AGENT:
        logger.error("second classification agent url not configured")
        raise HTTPException(status_code=500, detail="Agent URL not configured")
    try:
        resp = requests.post(
            SECOND_CLASSIFICATION_AGENT,
            json={"conversation_id": conv_id, "kb_id": kb_id, "summary": summary},
            timeout=AGENT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.error("second classification call failed: %s", exc)
        raise HTTPException(status_code=502, detail="Agent call failed")


def snow_create_interaction(conv_id: str, user_id: str) -> str:
    """Create a ServiceNow interaction for this conversation; return its id.

    Best-effort: on failure we log and return "" so a ServiceNow hiccup at the
    start of a conversation doesn't block the chat (incidents just won't be linked).
    """
    try:
        resp = run_agent_json(
            conv_id, SERVICENOW_AGENT, f"action=create_interaction | user={user_id}"
        )
        return resp.get("interaction_id", "") or ""
    except Exception:
        logger.exception("create_interaction failed")
        return ""


def snow_close_interaction(conv_id: str, interaction_id: str) -> None:
    """Close the interaction (non-IT case). Best-effort; failure is logged only."""
    try:
        run_agent(
            conv_id,
            SERVICENOW_AGENT,
            f"action=close_interaction | interaction_id={interaction_id}",
        )
    except Exception:
        logger.exception("close_interaction failed")


def snow_create_incident(conv_id: str, interaction_id, issue_type, details) -> dict:
    """Create an incident UNDER the interaction; return the agent's JSON.

    Passes interaction_id so the ServiceNow agent links the incident to the
    interaction. Returns {incident_id, message, ...}. Raises on failure (the
    endpoint catches it and shows the fallback) -- a failed incident is worth
    surfacing, unlike the best-effort interaction create/close.
    """
    return run_agent_json(
        conv_id,
        SERVICENOW_AGENT,
        f"action=create_incident | interaction_id={interaction_id} | "
        f"issue_type={issue_type} | details={details}",
    )


def snow_update_incident(conv_id: str, incident_id, note: str) -> None:
    """Add a progress update / work note to an existing incident. Best-effort.

    Called at later stages (diagnostics, troubleshoot, or a user 'no') to record
    progress on the incident opened at the start. Not shown to the user; a failed
    update is logged but never blocks the flow. No-op when there's no incident_id.
    """
    if not incident_id:
        return
    try:
        run_agent(
            conv_id,
            SERVICENOW_AGENT,
            f"action=update_incident | incident_id={incident_id} | note={note}",
        )
    except Exception:
        logger.exception("update_incident failed")


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
        resp = _post_multilingual({"agent": "detect", "message": text})
        if not resp.get("supported"):
            return fb  # unsupported language -> stay in the default language
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
                {"agent": "translate", "lang": lang, "message": m}
            )
            out.append(resp.get("reply") or m)
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
            {"agent": "translate", "lang": DEFAULT_LANG, "message": text}
        )
        return resp.get("reply") or text
    except Exception:
        logger.exception("translate_to_english failed")
        return text


def create_conversation():
    """Create a new, empty Foundry conversation."""
    return state["openai_client"].conversations.create()


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

    if stage is None or stage == AWAITING_ISSUE:
        result = _start(conv_id, user_message, vars, messages)
    elif stage == AWAITING_CLASSIFY:
        result = _run_classify(conv_id, user_message, vars, messages)
    elif stage == AWAITING_RESOLVED:
        result = _resolved_turn(conv_id, user_message, vars, messages)
    elif stage == AWAITING_PROCEED:
        result = _proceed_turn(conv_id, user_message, vars, messages)
    elif stage == AWAITING_FINAL:
        result = _final_turn(conv_id, user_message, vars, messages)
    else:
        raise HTTPException(status_code=409, detail="Conversation already completed")

    messages, new_stage, vars = result
    # The interaction represents the chat contact -> close it once the conversation
    # ends (any terminal), whether or not an incident was created/resolved.
    if (
        new_stage == DONE
        and vars.get("interaction_id")
        and not vars.get("interaction_closed")
    ):
        snow_close_interaction(conv_id, vars["interaction_id"])
        vars["interaction_closed"] = True
    return messages, new_stage, vars


def _start(conv_id, user_message, vars, messages):
    """First turn / greeting loop: open the interaction, classify, and route.

    The ServiceNow interaction is opened ONCE, at the start of the conversation
    (any category), and reused on greeting-loop turns. Routing by issue_type:
      greeting / non_it -> re-prompt for an Outlook issue up to MAX_NONACTIONABLE
                           times (shared counter), then end. No incident.
      non_outlook_it    -> create an incident (ticket), thank the user, end.
      outlook_it        -> create an incident up front, then enter classification.
    The interaction is closed centrally in step() when the conversation ends.
    """
    # Interaction is created once per conversation, at the start (reused on loops).
    if not vars.get("interaction_id"):
        vars["interaction_id"] = snow_create_interaction(
            conv_id, vars.get("user_id", "")
        )
    interaction_id = vars["interaction_id"]

    response = run_agent_json(conv_id, ORCHESTRATOR_AGENT, user_message)
    issue_type = response.get("issue_type")
    vars["issue_type"] = issue_type

    # Non-actionable (greeting or general/non-IT): re-prompt up to the cap, then end.
    if issue_type in ("greeting", "non_it"):
        count = vars.get("nonactionable_count", 0) + 1
        vars["nonactionable_count"] = count
        if count > MAX_NONACTIONABLE:
            messages.append(NONACTIONABLE_END_MESSAGE)
            return messages, DONE, vars
        messages.append(GREETING_PROMPT if issue_type == "greeting" else NON_IT_PROMPT)
        return messages, AWAITING_ISSUE, vars

    # non_outlook_it: create the ticket under the interaction, thank the user, end.
    if issue_type == "non_outlook_it":
        snow = snow_create_incident(conv_id, interaction_id, issue_type, user_message)
        vars["incident_id"] = snow.get("incident_id")
        ticket_msg = (
            snow.get("message")
            or f"Your ticket {snow.get('incident_id')} has been created."
        )
        messages.append(ticket_msg + " Thank you for contacting us.")
        return messages, DONE, vars

    # outlook_it: create the incident up front, then enter the classification flow.
    snow = snow_create_incident(conv_id, interaction_id, issue_type, user_message)
    vars["incident_id"] = snow.get("incident_id")
    messages.append(
        snow.get("message") or f"Incident {snow.get('incident_id')} has been created."
    )
    return _run_classify(conv_id, user_message, vars, messages)


def _run_classify(conv_id, user_message, vars, messages):
    """First-classification turn: run the FIRST classification agent.

    While it needs more info it returns chat_close=false with a follow-up question
    (agent_message); we show it and stay in AWAITING_CLASSIFY. When it returns
    chat_close=true we store its kb_id + summary and hand off to the second
    classification agent.
    """
    fc = run_agent_json(conv_id, FIRST_CLASSIFICATION_AGENT, user_message)

    if not fc.get("chat_close"):
        # still gathering info -> show the follow-up question and wait
        if fc.get("agent_message"):
            messages.append(fc["agent_message"])
        return messages, AWAITING_CLASSIFY, vars

    # classification complete -> keep kb_id + summary for the second agent
    vars["kb_id"] = fc.get("kb_id")
    vars["kb_summary"] = fc.get("summary")
    if fc.get("agent_message"):
        messages.append(fc["agent_message"])

    return _run_second_classification(conv_id, vars, messages)


def _run_second_classification(conv_id, vars, messages):
    """Second-classification decision: manual steps vs automated diagnostics.

    Sends the first agent's summary + kb_id to the SECOND classification agent.
    'manual' shows the steps and asks whether they resolved the issue
    (AWAITING_RESOLVED). Otherwise ('automate') it updates the incident (opened at
    the start), runs diagnostics, and asks to proceed (AWAITING_PROCEED).
    """
    sc = call_second_classification(conv_id, vars.get("kb_summary", ""), vars.get("kb_id"))
    mode = (sc.get("mode") or "").lower()
    vars["mode"] = mode

    if mode == "manual":
        if sc.get("steps"):
            messages.append(sc["steps"])
        if sc.get("agent_message"):
            messages.append(sc["agent_message"])
        messages.append("Did these steps resolve your issue?")
        return messages, AWAITING_RESOLVED, vars

    # automate: the incident was opened at the start -> UPDATE it (reached diagnostics)
    details = vars.get("kb_summary", "")
    snow_update_incident(
        conv_id, vars.get("incident_id"), "Automated diagnostics started."
    )
    messages.append("Diagnosis Flow started")
    messages.append(
        run_agent(
            conv_id,
            DIAGNOSTICS_AGENT,
            json.dumps({"kb_id": vars.get("kb_id"), "summary": details}),
        )
    )
    messages.append("Proceed with troubleshooting?")
    return messages, AWAITING_PROCEED, vars


def _resolved_turn(conv_id, user_message, vars, messages):
    """Handle the 'did the manual steps resolve it?' answer (manual path).

    'yes' resolves the incident opened at the start. 'no' updates that incident
    (it stays open for the team). Flow ends (DONE) either way.
    """
    if "yes" in user_message.lower():
        messages.append(
            run_agent(
                conv_id,
                SERVICENOW_AGENT,
                f"action=resolve | interaction_id={vars.get('interaction_id', '')} | "
                f"incident_id={vars.get('incident_id', '')}",
            )
        )
        messages.append("Glad it worked")
        return messages, DONE, vars
    # user said no -> update the incident, which stays open
    snow_update_incident(
        conv_id, vars.get("incident_id"), "Manual steps did not resolve the issue."
    )
    messages.append(
        f"Your incident {vars.get('incident_id', '')} stays open for the support team."
    )
    return messages, DONE, vars


def _proceed_turn(conv_id, user_message, vars, messages):
    """Handle the 'proceed with troubleshooting?' answer.

    'yes' runs the Troubleshoot agent and asks whether the issue is resolved
    (AWAITING_FINAL); anything else falls back to ServiceNow and ends (DONE).
    """
    if "yes" in user_message.lower():
        # reached troubleshoot -> update the incident, then run troubleshoot
        snow_update_incident(conv_id, vars.get("incident_id"), "Troubleshooting started.")
        messages.append("Troubleshoot started")
        messages.append(
            run_agent(conv_id, TROUBLESHOOT_AGENT, vars.get("kb_summary", ""))
        )
        messages.append("Issue Resolved?")
        return messages, AWAITING_FINAL, vars
    # user said no -> update the incident, which stays open
    snow_update_incident(conv_id, vars.get("incident_id"), "User declined troubleshooting.")
    messages.append(
        f"No problem. Your incident {vars.get('incident_id', '')} stays open for the "
        "support team."
    )
    return messages, DONE, vars


def _final_turn(conv_id, user_message, vars, messages):
    """Handle the final 'issue resolved?' answer after troubleshooting.

    'yes' resolves the ServiceNow incident; anything else falls back to ServiceNow
    (e.g. escalate/keep open). Flow ends (DONE) either way.
    """
    if "yes" in user_message.lower():
        messages.append(
            run_agent(
                conv_id,
                SERVICENOW_AGENT,
                f"action=resolve | interaction_id={vars.get('interaction_id', '')} | "
                f"incident_id={vars.get('incident_id', '')}",
            )
        )
        return messages, DONE, vars
    # user said no -> update the incident, which stays open
    snow_update_incident(
        conv_id, vars.get("incident_id"), "Issue not resolved after troubleshooting."
    )
    messages.append(
        f"Your incident {vars.get('incident_id', '')} stays open for the support team."
    )
    return messages, DONE, vars


# ---------------------------------------------------------------------------
# Persistence: conversations + sessions tables (Azure SQL)
# ---------------------------------------------------------------------------
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
    vars: dict,
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
        "vars": vars,
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


@app.get("/")
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


@app.post("/chat")
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
        messages, stage, vars = step(
            conversation.id, request.message, None, {"user_id": request.user_id}
        )
        messages = translate_messages(messages, vars.get("lang"))
        answer = "\n\n".join(m for m in messages if m)
        save_conversation(
            conn,
            request.user_id,
            conversation.id,
            stage=stage,
            vars=vars,
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


@app.post("/chat/continue")
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
        vars = conversation.get("vars", {})

        # Ended conversations are final -- no rollover, no memory carry-over. The
        # frontend should start a new chat (POST /chat) instead of continuing.
        if stage == DONE:
            raise HTTPException(
                status_code=409,
                detail="This conversation has ended. Please start a new chat.",
            )

        # normal in-flow turn (active conversation)
        messages, new_stage, new_vars = step(conv_id, request.message, stage, vars)
        messages = translate_messages(messages, new_vars.get("lang"))
        answer = "\n\n".join(m for m in messages if m)
        save_conversation(
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
        # Exclude ended (DONE) conversations so they drop out of history.
        if session_id:
            cur.execute(
                f"SELECT {cols} FROM conversations WHERE user_id = ? AND session_id = ? "
                "AND (stage IS NULL OR stage <> 'DONE') ORDER BY seq ASC",
                (user_id, session_id),
            )
        elif SQLITE_DB_PATH:
            cur.execute(
                f"SELECT {cols} FROM conversations WHERE user_id = ? "
                "AND (stage IS NULL OR stage <> 'DONE') "
                "ORDER BY created_at DESC LIMIT 10",
                (user_id,),
            )
        else:
            cur.execute(
                f"SELECT TOP 10 {cols} FROM conversations "
                "WHERE user_id = ? AND (stage IS NULL OR stage <> 'DONE') "
                "ORDER BY created_at DESC",
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
