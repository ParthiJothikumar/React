"""Dependency wiring -- the one place objects are constructed.

Two lifetimes:

  PROCESS-WIDE singletons, built once at import: the clients. They hold pooled
  requests.Sessions and the lazily built Foundry client, which is exactly what must be
  reused across requests -- rebuilding them per request is what caused the SNAT-port
  problem this refactor fixes. SupportFlow joins them (stateless, holds only clients).

  PER-REQUEST objects, built by the Depends providers: the repositories and services,
  because each is bound to one DB connection that must be closed when the response is
  sent. get_db() is a generator dependency, so FastAPI runs the `finally` for us and
  the controllers no longer need their own try/finally.

Because every service takes its collaborators as arguments, a test can build one
directly with fakes and never touch this module.
"""
from fastapi import Depends

from app.clients.agents import AgentClient
from app.clients.foundry import FoundryClient
from app.clients.jobs_agent import JobsClient
from app.clients.multilingual import MultilingualClient
from app.clients.servicenow import ServiceNowClient
from app.config import settings
from app.db import Database
from app.flow import SupportFlow
from app.repositories import ConversationRepository, SessionRepository
from app.services import ChatService, HistoryService, JobService

# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
database = Database(settings)

agent_client = AgentClient(
    timeout=settings.AGENT_HTTP_TIMEOUT,
    second_classification_url=settings.SECOND_CLASSIFICATION_AGENT,
    pool_maxsize=settings.HTTP_POOL_MAXSIZE,
)
multilingual_client = MultilingualClient(
    agent_url=settings.MULTILINGUAL_AGENT,
    default_lang=settings.DEFAULT_LANG,
    timeout=settings.AGENT_HTTP_TIMEOUT,
    pool_maxsize=settings.HTTP_POOL_MAXSIZE,
)
jobs_client = JobsClient(
    timeout=settings.AGENT_HTTP_TIMEOUT,
    pool_maxsize=settings.HTTP_POOL_MAXSIZE,
)
servicenow_client = ServiceNowClient(agent_client, settings.SERVICENOW_AGENT)
foundry_client = FoundryClient(
    settings.AZURE_FOUNDRY_PROJECT_ENDPOINT,
    timeout=settings.FOUNDRY_HTTP_TIMEOUT,
    max_retries=settings.FOUNDRY_MAX_RETRIES,
)

support_flow = SupportFlow(
    agents=agent_client,
    servicenow=servicenow_client,
    multilingual=multilingual_client,
    settings=settings,
)


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


def get_chat_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
    sessions: SessionRepository = Depends(get_session_repo),
) -> ChatService:
    return ChatService(
        conversations=conversations,
        sessions=sessions,
        flow=support_flow,
        foundry=foundry_client,
        jobs=jobs_client,
        multilingual=multilingual_client,
    )


def get_job_service(
    conversations: ConversationRepository = Depends(get_conversation_repo),
) -> JobService:
    return JobService(
        conversations=conversations,
        flow=support_flow,
        jobs=jobs_client,
        multilingual=multilingual_client,
        settings=settings,
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
    """
    return JobService(
        conversations=ConversationRepository(conn, use_sqlite=database.use_sqlite),
        flow=support_flow,
        jobs=jobs_client,
        multilingual=multilingual_client,
        settings=settings,
    )
