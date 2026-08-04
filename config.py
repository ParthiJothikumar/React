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
DONE = "DONE"

# Diagnostics/Troubleshoot run as long-running ASYNC jobs (3-15 min, Intune on the
# client machine). The flow does NOT block: it starts the job and enters one of these
# "running" stages; the FE polls GET /jobs/status until the job finishes, then the
# flow advances. See app/worker.py (the background worker) and queue_worker/.
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
# Job kinds -- which long-running run a job represents.
JOB_KIND_DIAGNOSTIC = "diagnostic"
JOB_KIND_TROUBLESHOOT = "troubleshoot"

# The queue that drives the polling loop. A message on it triggers queue_worker,
# which polls the agent's /status and RE-ENQUEUES itself with a delay until done.
# Lives in the same storage account as the Function App (AzureWebJobsStorage).
JOBS_QUEUE_NAME = os.getenv("JOBS_QUEUE_NAME", "diag-jobs")
JOBS_QUEUE_CONNECTION = os.getenv("AzureWebJobsStorage", "")

# How often the worker re-checks the agent's /status, and the safety cap that stops
# a stuck/offline job from looping forever (POLL_DELAY * MAX_TRIES ~= max wait).
JOB_POLL_DELAY_SECONDS = int(os.getenv("JOB_POLL_DELAY_SECONDS", "30"))
JOB_MAX_TRIES = int(os.getenv("JOB_MAX_TRIES", "40"))  # 40 * 30s ~= 20 min

# The troubleshoot/diagnostic Function Apps (owned by other teams) expose the async
# contract: POST <start> -> job_id, GET <status>?job_id -> {state, progress, result}.
# Fall back to the existing *_AGENT_URL if a dedicated start/status URL isn't set.
DIAGNOSTICS_START_URL = os.getenv("DIAGNOSTICS_START_URL", DIAGNOSTICS_AGENT)
DIAGNOSTICS_STATUS_URL = os.getenv("DIAGNOSTICS_STATUS_URL", "")
TROUBLESHOOT_START_URL = os.getenv("TROUBLESHOOT_START_URL", TROUBLESHOOT_AGENT)
TROUBLESHOOT_STATUS_URL = os.getenv("TROUBLESHOOT_STATUS_URL", "")

# While the other teams' async /start + /status don't exist yet, run in DUMMY mode:
# start_job returns a fake job_id and check_status simulates progress so the whole
# enqueue -> poll -> done loop runs locally. Set JOBS_DUMMY=false once they're ready.
JOBS_DUMMY = os.getenv("JOBS_DUMMY", "true").lower() == "true"
