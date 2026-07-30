"""ServiceNow helpers (interaction + incident lifecycle)."""
from app.clients.agents import run_agent, run_agent_json
from app.config import SERVICENOW_AGENT, logger


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
