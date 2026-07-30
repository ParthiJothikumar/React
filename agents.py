"""Agent HTTP helpers (POST to the sibling agent Function Apps)."""
import requests
from fastapi import HTTPException

from app.config import AGENT_HTTP_TIMEOUT, SECOND_CLASSIFICATION_AGENT, logger


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
