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


def call_second_classification(conv_id, kb_id, message="") -> dict:
    """Ask the SECOND classification agent how to resolve the issue.

    Posts {conversation_id, kb_id, message} ("" on the first call, the user's reply
    on ask_user follow-ups -- the agent owns the follow-up count) and returns its
    ValidationResponse dict. NEVER raises: any transport/HTTP failure becomes a
    {success: false, error: <cause>} dict so the flow router handles every failure
    the same way (show a user-safe line, stash the raw cause).
    """
    body = {"conversation_id": conv_id, "kb_id": kb_id, "message": message}
    if not SECOND_CLASSIFICATION_AGENT:
        logger.error("second classification agent url not configured")
        return _second_class_failure("Agent URL not configured")
    try:
        resp = requests.post(
            SECOND_CLASSIFICATION_AGENT, json=body, timeout=AGENT_HTTP_TIMEOUT
        )
    except requests.RequestException as exc:
        logger.error("second classification call failed conv_id=%s: %s", conv_id, exc)
        return _second_class_failure(str(exc))
    try:
        data = resp.json()
    except ValueError:
        data = None
    # Use the agent's own JSON body even on a non-2xx (its HTTP-layer errors carry a
    # {success: false, ...} body); otherwise synthesize a failure from the status.
    if isinstance(data, dict) and (resp.ok or "success" in data or "error" in data):
        return data
    return _second_class_failure(
        f"HTTP {resp.status_code} from second classification agent"
    )


def _second_class_failure(error: str) -> dict:
    """Synthesize a ValidationResponse-shaped failure. agent_message is left empty so
    the flow shows its generic fallback; the raw cause goes in `error` (logs/DB)."""
    return {
        "success": False,
        "validated": False,
        "action": None,
        "agent_message": "",
        "error": error,
    }
