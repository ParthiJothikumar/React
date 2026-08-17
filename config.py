"""Settings (environment / App Settings) and the shared logger.

Every environment-derived value lives on the Settings class below, read once into the
module-level `settings` singleton. Domain constants that are NOT configuration (stage
names, agent vocabularies, fixed prompts) live in constants.py.

Why a class instead of ~20 module-level os.getenv() calls:
  * a test can build Settings({"JOBS_DUMMY": "false", ...}) instead of reimporting
    the module to change one value;
  * parsing and defaults live in one place, so "was this an int or a string?" has one
    answer;
  * warn_on_risky_config() gives us a single startup check -- which is what catches a
    misconfiguration (e.g. JOBS_DUMMY left on in production) instead of it silently
    defaulting and telling real users their machine was repaired.

No secrets or URLs are hardcoded; see local.settings.json for the full list.
"""
import logging
import os
from typing import Mapping, Optional

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


def _csv(raw: str) -> list:
    """Split a comma-separated App Setting into a clean list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _flag(raw: str) -> bool:
    """Parse a boolean App Setting (they arrive as strings)."""
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(raw: str, default: int) -> int:
    """Parse an int App Setting, falling back rather than crashing on junk."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("expected an integer setting, got %r; using %s", raw, default)
        return default


class Settings:
    """All environment-derived configuration, parsed once.

    `env` defaults to os.environ; pass a plain dict in tests to build a Settings with
    whatever values that test needs.
    """

    def __init__(self, env: Optional[Mapping[str, str]] = None):
        get = (env if env is not None else os.environ).get

        # -- Frontend / transport -------------------------------------------
        self.CORS_ORIGINS = _csv(
            get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        )
        # Max seconds to wait for an agent Function App to respond.
        self.AGENT_HTTP_TIMEOUT = _int(get("AGENT_HTTP_TIMEOUT", "120"), 120)

        # -- Agent Function App URLs ----------------------------------------
        # Each agent runs in its OWN Function App; these hold that app's URL (the
        # client POSTs to it) -- they are not agent names. If an app uses a function
        # key, put the full URL incl. ?code=<key> here.
        self.ORCHESTRATOR_AGENT = get("ORCHESTRATOR_AGENT_URL", "")
        # Two-stage classification (each its own Function App):
        #   FIRST  -> classifies the issue and asks follow-ups. Response:
        #             follow-up: {"chat_close": false, "kb_id": null, "summary": null,
        #                         "agent_message": "<next question>"}
        #             done:      {"chat_close": true, "kb_id": "kb100",
        #                         "summary": "...", "agent_message": ""}
        #   SECOND -> given {summary, kb_id}, decides how to resolve. Response:
        #             {"mode": "manual" | "automate", "steps": "...",
        #              "agent_message": "..."}
        self.FIRST_CLASSIFICATION_AGENT = get("FIRST_CLASSIFICATION_AGENT_URL", "")
        self.SECOND_CLASSIFICATION_AGENT = get("SECOND_CLASSIFICATION_AGENT_URL", "")
        self.SERVICENOW_AGENT = get("SERVICENOW_AGENT_URL", "")
        self.DIAGNOSTICS_AGENT = get("DIAGNOSTICS_AGENT_URL", "")
        self.TROUBLESHOOT_AGENT = get("TROUBLESHOOT_AGENT_URL", "")

        # -- Multilingual agent ---------------------------------------------
        # Its own Function App: detects the user's language and translates outgoing
        # messages into it. STATELESS -- it runs on the Agents API and creates its OWN
        # thread per request, so we do NOT pass a conversation id and it never touches
        # our Foundry conversation. Contract (structured JSON body):
        #   detect    -> {"agent": "detect", "message": "<text>"}
        #                returns {"code": "fr", "supported": true, ...}
        #   translate -> {"agent": "translate", "lang": "<ISO>", "message": "<text>"}
        #                returns {"reply": "<text in target language>"}
        self.MULTILINGUAL_AGENT = get("MULTILINGUAL_AGENT_URL", "")
        # Language assumed when detection is unavailable/uncertain/unsupported.
        # Outgoing messages are NOT translated when the detected language equals this.
        self.DEFAULT_LANG = get("DEFAULT_LANG", "en")

        # -- Flow policy ----------------------------------------------------
        self.MAX_NONACTIONABLE = _int(get("MAX_NONACTIONABLE", "2"), 2)

        # -- State store ----------------------------------------------------
        # If SQLITE_DB_PATH is set, use a LOCAL SQLite file instead of Azure SQL.
        # Handy for testing on a machine that can't reach Azure SQL. Leave it empty
        # to use the real Azure SQL connection (SQL_CONNECTION_STRING).
        self.SQLITE_DB_PATH = get("SQLITE_DB_PATH", "")
        self.SQL_CONNECTION_STRING = get("SQL_CONNECTION_STRING", "")

        # -- Azure SQL connection pool ---------------------------------------
        # mssql-python pools by default (max_size=100, idle_timeout=600). We set
        # it explicitly so the intended ceiling is visible in the code, and so
        # raising the threadpool later can't silently open hundreds of sessions.
        #
        # SIZING: a request holds exactly ONE connection (get_db is cached per
        # request, so both repositories share it), and only a threadpool worker
        # can hold one -- so the worker count (40 by default) covers the request
        # path entirely. The few spare are for connections taken OUTSIDE the
        # threadpool: the job-reaper timer, and any health check that queries SQL.
        #
        # The pool MUST be >= the worker count: mssql-python raises immediately
        # when the pool is exhausted, it does not queue and wait (there is no
        # pool_timeout equivalent). A pool smaller than the worker count turns a
        # brief shortage into failed requests.
        #
        # TOTAL sessions against Azure SQL = max_size x processes x instances.
        # Check the tier's max concurrent sessions before raising either number.
        self.SQL_POOL_MAX_SIZE = _int(get("SQL_POOL_MAX_SIZE", "45"), 45)
        # Seconds a connection may sit UNUSED before it is closed and dropped
        # from the pool -- housekeeping, so we don't hold connections open all
        # night. This is NOT a "wait for a free connection" timeout.
        self.SQL_POOL_IDLE_TIMEOUT = _int(get("SQL_POOL_IDLE_TIMEOUT", "300"), 300)

        # -- Azure AI Foundry ------------------------------------------------
        self.AZURE_FOUNDRY_PROJECT_ENDPOINT = get(
            "AZURE_FOUNDRY_PROJECT_ENDPOINT", ""
        )
        # Seconds allowed for ONE Foundry request. Without this the SDK's own default
        # applies, which left the first call of every new chat unbounded -- the one
        # gap in the per-turn timeout budget.
        #
        # WORST CASE = FOUNDRY_HTTP_TIMEOUT x (1 + FOUNDRY_MAX_RETRIES). The OpenAI SDK
        # retries twice by default, so a 30s timeout would really mean 90s. Both are set
        # explicitly so the ceiling is visible and can be reasoned about.
        # We only call conversations.create() (creating an empty conversation, normally
        # sub-second). If a long-running call such as responses.create() is ever routed
        # through this client, give that call its own per-request timeout instead of
        # raising this one.
        self.FOUNDRY_HTTP_TIMEOUT = _int(get("FOUNDRY_HTTP_TIMEOUT", "20"), 20)
        self.FOUNDRY_MAX_RETRIES = _int(get("FOUNDRY_MAX_RETRIES", "1"), 1)

        # -- Async job settings ---------------------------------------------
        # No backend queue/loop: the flow starts the job and stores its job_id; the FE
        # polls GET /jobs/status, and that endpoint calls the agent's /status live,
        # updates the DB, and advances the stage when the job finishes.
        #
        # Give up after JOB_MAX_POLLS status polls without completion, so a stuck /
        # offline job can't hold the conversation in a RUNNING stage forever. Within
        # each poll, retry the status check up to JOB_STATUS_RETRIES times to ride out
        # a transient blip.
        self.JOB_MAX_POLLS = _int(get("JOB_MAX_POLLS", "5"), 5)
        self.JOB_STATUS_RETRIES = _int(get("JOB_STATUS_RETRIES", "3"), 3)

        # The troubleshoot/diagnostic Function Apps (owned by other teams) expose the
        # async contract: POST <start> -> job_id, GET <status>?job_id -> {state,
        # progress, result}. Fall back to the *_AGENT_URL if a dedicated start URL
        # isn't set.
        self.DIAGNOSTICS_START_URL = get(
            "DIAGNOSTICS_START_URL", self.DIAGNOSTICS_AGENT
        )
        self.DIAGNOSTICS_STATUS_URL = get("DIAGNOSTICS_STATUS_URL", "")
        self.TROUBLESHOOT_START_URL = get(
            "TROUBLESHOOT_START_URL", self.TROUBLESHOOT_AGENT
        )
        self.TROUBLESHOOT_STATUS_URL = get("TROUBLESHOOT_STATUS_URL", "")

        # NOTE: there is deliberately NO "simulate the jobs" switch, and no fallback for
        # an unset URL. JobsClient raises instead. The old JOBS_DUMMY flag defaulted to
        # true and start()/check_status() also faked a result whenever a URL was blank --
        # so one missing App Setting made the app tell real users "No issues found;
        # Outlook profile repaired" with nothing having run, and resolve their ServiceNow
        # incident when they confirmed. Tests inject a scripted stand-in for JobsClient.

        # How many outbound sockets each client keeps alive per host. One pooled
        # Session per client (see clients/) reuses connections instead of opening a
        # fresh TCP+TLS one per call, which would burn the instance's ~128 SNAT ports.
        self.HTTP_POOL_MAXSIZE = _int(get("HTTP_POOL_MAXSIZE", "50"), 50)

    @property
    def use_sqlite(self) -> bool:
        """True when we're pointed at a local SQLite file instead of Azure SQL."""
        return bool(self.SQLITE_DB_PATH)

    def warn_on_risky_config(self) -> None:
        """Log loudly about settings that are fine locally but wrong in production.

        Called once from main.py at import. These are warnings, not hard failures, so
        local development still runs with an empty configuration -- but a production
        instance leaves an unmissable trail in Application Insights.
        """
        for name in ("DIAGNOSTICS_START_URL", "DIAGNOSTICS_STATUS_URL",
                     "TROUBLESHOOT_START_URL", "TROUBLESHOOT_STATUS_URL"):
            if not getattr(self, name):
                logger.warning(
                    "%s not configured: that job type will fail with an error rather "
                    "than run (by design -- it no longer simulates a result)", name,
                )
        if self.use_sqlite:
            logger.warning(
                "SQLITE_DB_PATH is set (%s): state is on the per-instance temp disk "
                "and is lost on restart/scale-out. Use SQL_CONNECTION_STRING in "
                "production.", self.SQLITE_DB_PATH,
            )
        missing = [
            name
            for name in (
                "ORCHESTRATOR_AGENT",
                "FIRST_CLASSIFICATION_AGENT",
                "SECOND_CLASSIFICATION_AGENT",
                "SERVICENOW_AGENT",
            )
            if not getattr(self, name)
        ]
        if missing:
            logger.warning("agent URLs not configured: %s", ", ".join(missing))
        if not self.AZURE_FOUNDRY_PROJECT_ENDPOINT:
            logger.warning("AZURE_FOUNDRY_PROJECT_ENDPOINT not configured")


# The process-wide instance every layer reads. Tests build their own Settings and
# inject it rather than mutating this one.
settings = Settings()
