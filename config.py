"""Configuration & constants.

All configuration is read from environment variables / App Settings (see
local.settings.json for the full list). No secrets or URLs are hardcoded. Importing
this module performs the app-wide bootstrap (load_dotenv + logging) exactly once and
exposes the shared `logger`; every other module imports its settings/constants and the
logger from here.
"""
import logging
import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Application bootstrap: environment variables + logging
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("orchestrator_api")

# ---------------------------------------------------------------------------
# Configuration & constants (from env; no hardcoded secrets/URLs)
# ---------------------------------------------------------------------------
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
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
# ask_user follow-up turn (chat-await, NOT a RUNNING stage): the second
# classification agent asked a question and we're waiting for the user's reply.
AWAITING_FOLLOWUP = "AWAITING_FOLLOWUP"
DONE = "DONE"

# Diagnostics/Troubleshoot run as long-running ASYNC jobs (3-15 min, Intune on the
# client machine). The flow does NOT block: it starts the job and enters one of these
# "running" stages; the FE polls GET /jobs/status, which checks the agent's /status
# live and advances the flow when the job finishes.
DIAGNOSTICS_RUNNING = "DIAGNOSTICS_RUNNING"
TROUBLESHOOT_RUNNING = "TROUBLESHOOT_RUNNING"
RUNNING_STAGES = frozenset({DIAGNOSTICS_RUNNING, TROUBLESHOOT_RUNNING})

# Allowed issue_type values returned by the ORCHESTRATOR agent. Anything outside
# this set is classification drift: it is logged and handled as non_it (re-prompt)
# so an unexpected label can never silently create an incident.
ISSUE_GREETING = "greeting"
ISSUE_NON_IT = "non_it"
ISSUE_NON_OUTLOOK_IT = "non_outlook_it"
ISSUE_OUTLOOK_IT = "outlook_it"
VALID_ISSUE_TYPES = frozenset(
    {ISSUE_GREETING, ISSUE_NON_IT, ISSUE_NON_OUTLOOK_IT, ISSUE_OUTLOOK_IT}
)

# Allowed mode values returned by the SECOND classification agent. An unexpected
# value is logged and handled as MODE_MANUAL (show steps, ask the user) so a drift
# can never silently trigger automated diagnostics.
MODE_MANUAL = "manual"
MODE_AUTOMATE = "automate"
VALID_MODES = frozenset({MODE_MANUAL, MODE_AUTOMATE})

# action values returned by the SECOND classification agent's richer contract
# (ValidationResponse). Note the automate value is "automatic" here (not "automate").
ACTION_MANUAL = "manual"
ACTION_AUTOMATIC = "automatic"
# Single user-safe line shown on a failure that carries no agent_message. The raw
# technical `error` is stored in flow_vars["last_error"] / logged, never shown.
SECOND_CLASS_FALLBACK = (
    "Sorry, we couldn't complete this automatically. A support team member "
    "will follow up to help."
)

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

# ---------------------------------------------------------------------------
# State store selection
# ---------------------------------------------------------------------------
# If SQLITE_DB_PATH is set, use a LOCAL SQLite file instead of Azure SQL.
# Handy for testing on a machine that can't reach Azure SQL. Leave it empty to
# use the real Azure SQL connection (SQL_CONNECTION_STRING).
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "")

# ---------------------------------------------------------------------------
# Async job settings (diagnostics / troubleshoot long-running runs)
# ---------------------------------------------------------------------------
# No backend queue/loop: the flow starts the job and stores its job_id; the FE polls
# GET /jobs/status every ~30s, and that endpoint calls the agent's /status live,
# updates the DB, and advances the stage when the job finishes.
JOB_KIND_DIAGNOSTIC = "diagnostic"
JOB_KIND_TROUBLESHOOT = "troubleshoot"

# Give up after JOB_MAX_POLLS status polls without completion, so a stuck/offline job
# can't hold the conversation in a RUNNING stage forever. Within each poll, retry the
# status check up to JOB_STATUS_RETRIES times to ride out a transient blip.
JOB_MAX_POLLS = int(os.getenv("JOB_MAX_POLLS", "5"))
JOB_STATUS_RETRIES = int(os.getenv("JOB_STATUS_RETRIES", "3"))

# The troubleshoot/diagnostic Function Apps (owned by other teams) expose the async
# contract: POST <start> -> job_id, GET <status>?job_id -> {state, progress, result}.
# Fall back to the existing *_AGENT_URL if a dedicated start/status URL isn't set.
DIAGNOSTICS_START_URL = os.getenv("DIAGNOSTICS_START_URL", DIAGNOSTICS_AGENT)
DIAGNOSTICS_STATUS_URL = os.getenv("DIAGNOSTICS_STATUS_URL", "")
TROUBLESHOOT_START_URL = os.getenv("TROUBLESHOOT_START_URL", TROUBLESHOOT_AGENT)
TROUBLESHOOT_STATUS_URL = os.getenv("TROUBLESHOOT_STATUS_URL", "")

# While the other teams' async /start + /status don't exist yet, run in DUMMY mode:
# start_job returns a fake job_id and check_status simulates progress across polls so
# the whole start -> poll -> done flow runs locally. Set JOBS_DUMMY=false when ready.
JOBS_DUMMY = os.getenv("JOBS_DUMMY", "true").lower() == "true"
