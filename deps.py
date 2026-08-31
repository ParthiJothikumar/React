"""Dependency wiring -- the one place objects are constructed.

Two lifetimes:

  PROCESS-WIDE, built once at import: the pooled requests.Sessions and the lazily built
  Foundry client. These are exactly what must be reused across requests -- rebuilding
  them per request is what caused the SNAT-port problem this refactor fixes.

  PER-REQUEST, built by the Depends providers: the repositories, the clients, the flow
  and the services. The repositories because each is bound to one DB connection that
  must be closed when the response is sent (get_db is a generator dependency, so FastAPI
  runs the `finally` for us). The clients because each carries this request's CallTrace,
  and request 1's rows must not land in request 2's list -- they are handed the shared
  sessions above, so building them costs nothing but a few references.

Because every service takes its collaborators as arguments, a test can build one
directly with fakes and never touch this module.
"""
from fastapi import Depends

from app.clients.agents import AgentClient
from app.clients.foundry import FoundryClient
from app.clients.http import HttpClient
from app.clients.jobs_agent import JobsClient
from app.clients.multilingual import MultilingualClient
from app.clients.servicenow import ServiceNowClient
from app.config import settings
from app.db import Database
from app.flow import SupportFlow
from app.repositories import (
    AgentCallRepository,
    ConversationRepository,
    SessionRepository,
)
from app.services import ChatService, HistoryService, JobService
from app.tracing import CallTrace, agent_names

# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
database = Database(settings)

# The connection POOLS -- the one thing that genuinely must be process-wide. Built here
# rather than inside a client, because the per-request clients below are handed the same
# session objects: that is what keeps the sockets (and their TCP+TLS handshakes) reused
# across every user, instead of one pool per request exhausting the host's ~128 SNAT
# ports.
#
# Three pools, not one, on purpose: pool_maxsize is counted per HOST, and the diagnostics
# and troubleshoot agents share a host -- so three pools give that host three times the
# headroom a single shared one would.
agent_session = HttpClient.build_session(settings.HTTP_POOL_MAXSIZE)
multilingual_session = HttpClient.build_session(settings.HTTP_POOL_MAXSIZE)
jobs_session = HttpClient.build_session(settings.HTTP_POOL_MAXSIZE)

foundry_client = FoundryClient(
    settings.AZURE_FOUNDRY_PROJECT_ENDPOINT,
    timeout=settings.FOUNDRY_HTTP_TIMEOUT,
    max_retries=settings.FOUNDRY_MAX_RETRIES,
)

# URL -> agent label for the agent_calls rows. Built once: the URLs never change.
AGENT_NAMES = agent_names(settings)


# ---------------------------------------------------------------------------
# Per-request providers
# ---------------------------------------------------------------------------
def get_db():
    """Open a connection for this request and close it once the response is sent."""
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_conversation_repo(conn=Depends(get_db)) -> ConversationRepository:
    return ConversationRepository(conn, use_sqlite=database.use_sqlite)


def get_session_repo(conn=Depends(get_db)) -> SessionRepository:
    return SessionRepository(conn, use_sqlite=database.use_sqlite)


def get_agent_call_repo(conn=Depends(get_db)) -> AgentCallRepository:
    return AgentCallRepository(conn)


def _clients_for(trace: CallTrace):
    """This request's clients: the SHARED pools, plus this request's trace.

    Cheap to build. Each client is a handful of references -- the session is passed in,
    so HttpClient.build_session is never called here (it only runs when no session is
    given). No TCP connection, no handshake, nothing pooled is recreated.

    What must be per-request is the trace: request 1's rows must not land in request 2's
    list. Since a client holds its trace, the client is per-request too. SupportFlow
    holds no state of its own, so building one is four assignments.
    """
    agents = AgentClient(
        timeout=settings.AGENT_HTTP_TIMEOUT,
        second_classification_url=settings.SECOND_CLASSIFICATION_AGENT,
        session=agent_session,
        trace=trace,
    )
    lang = MultilingualClient(
        agent_url=settings.MULTILINGUAL_AGENT,
        default_lang=settings.DEFAULT_LANG,
        timeout=settings.AGENT_HTTP_TIMEOUT,
        session=multilingual_session,
        trace=trace,
    )
    jobs = JobsClient(
        timeout=settings.AGENT_HTTP_TIMEOUT,
        session=jobs_session,
        trace=trace,
    )
    flow = SupportFlow(
        agents=agents,
        servicenow=ServiceNowClient(agents, settings.SERVICENOW_AGENT),
        multilingual=lang,
        settings=settings,
    )
    return flow, lang, jobs


def get_chat_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
    agent_calls: AgentCallRepository = Depends(get_agent_call_repo),
) -> ChatService:
    trace = CallTrace(AGENT_NAMES)
    flow, lang, jobs = _clients_for(trace)
    return ChatService(
        conversations=conversations,
        sessions=sessions,
        flow=flow,
        foundry=foundry_client,
        jobs=jobs,
        multilingual=lang,
        agent_calls=agent_calls,
        trace=trace,
    )


def get_job_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    agent_calls: AgentCallRepository = Depends(get_agent_call_repo),
) -> JobService:
    trace = CallTrace(AGENT_NAMES)
    flow, lang, jobs = _clients_for(trace)
    return JobService(
        conversations=conversations,
        flow=flow,
        jobs=jobs,
        multilingual=lang,
        settings=settings,
        agent_calls=agent_calls,
        trace=trace,
    )


def get_history_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
) -> HistoryService:
    return HistoryService(conversations=conversations, sessions=sessions)


# ---------------------------------------------------------------------------
# Non-HTTP entry point
# ---------------------------------------------------------------------------
def make_job_service(conn) -> JobService:
    """Build a JobService from a bare connection, with no FastAPI involved.

    The Depends providers above only work inside a request. The reaper timer has no
    request, so it needs this -- and being able to add it at all is the point of keeping
    the service layer free of HTTP: the timer calls exactly the same code the endpoint
    does.

    The reaper gets its own trace per sweep, so a job it completes is recorded like any
    other -- otherwise the calls that finish abandoned jobs would be the ones missing
    from the table.
    """
    trace = CallTrace(AGENT_NAMES)
    flow, lang, jobs = _clients_for(trace)
    return JobService(
        conversations=ConversationRepository(conn, use_sqlite=database.use_sqlite),
        flow=flow,
        jobs=jobs,
        multilingual=lang,
        settings=settings,
        agent_calls=AgentCallRepository(conn),
        trace=trace,
    )
