"""Client for the diagnostic/troubleshoot Function Apps' ASYNC contract.

Those apps are owned by other teams and (will) expose:
    POST <start_url>                 -> { "job_id": "..." }         (returns immediately)
    GET  <status_url>?job_id=<id>    -> { "state": "running|done|failed",
                                          "progress": "...", "result": "..." }

Until those endpoints exist, JOBS_DUMMY=true makes start_job return a fake id and
check_status simulate progress, so the whole enqueue -> poll -> done loop runs
locally. Flip JOBS_DUMMY=false (and set the *_START_URL / *_STATUS_URL) when the
teams are ready -- no other code changes.
"""
import uuid

import requests

from app.config import AGENT_HTTP_TIMEOUT, JOBS_DUMMY, logger

# Fake progress used ONLY in dummy mode so you can watch the loop tick.
_DUMMY_STAGES = [
    "Checking Outlook profile",
    "Testing mailbox connectivity",
    "Scanning OST file",
    "Repairing profile",
    "Finalising results",
]


def start_job(kind: str, start_url: str, conv_id: str, payload: str) -> str:
    """Kick off the long-running run and return its job_id (does NOT wait)."""
    if JOBS_DUMMY or not start_url:
        job_id = "job_" + uuid.uuid4().hex[:12]
        logger.info("start_job DUMMY kind=%s conv_id=%s -> %s", kind, conv_id, job_id)
        return job_id

    resp = requests.post(
        start_url,
        json={"conversation_id": conv_id, "input": payload},
        timeout=AGENT_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["job_id"]


def check_status(kind: str, status_url: str, job_id: str, tries: int) -> dict:
    """Ask the agent how the job is going. Returns {state, progress, result}.

    `tries` is how many times we've already checked -- used only by the dummy to
    advance through the fake stages so the loop eventually completes.
    """
    if JOBS_DUMMY or not status_url:
        if tries >= len(_DUMMY_STAGES):
            return {"state": "done", "progress": "Complete.",
                    "result": "No issues found; Outlook profile repaired."}
        return {"state": "running",
                "progress": f"{_DUMMY_STAGES[tries]} ({tries + 1}/{len(_DUMMY_STAGES)})",
                "result": None}

    resp = requests.get(
        status_url, params={"job_id": job_id}, timeout=AGENT_HTTP_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    # Normalise to {state, progress, result} regardless of their exact key names.
    return {
        "state": (data.get("state") or data.get("status") or "running"),
        "progress": data.get("progress") or data.get("message") or "Working...",
        "result": data.get("result"),
    }
