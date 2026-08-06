"""Azure AI Foundry client (lazy init) and Foundry conversation helpers.

Azure Functions does not run FastAPI's lifespan, so the Foundry client is created on
the FIRST request via _ensure_state() and cached in the module-level `state` dict for
the worker process. (When this service moves to Container Apps, replace this lazy
singleton with a FastAPI lifespan startup hook -- see the migration notes.)
"""
import os

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from app.config import logger

# ---------------------------------------------------------------------------
# Foundry client state (lazy init, cached per worker process)
# ---------------------------------------------------------------------------
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


def create_conversation():
    """Create a new, empty Foundry conversation."""
    return state["openai_client"].conversations.create()


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
