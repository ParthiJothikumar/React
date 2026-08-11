"""Client for the diagnostic/troubleshoot Function Apps' ASYNC contract.

Those apps are owned by other teams and expose:
    POST <start_url>              -> { "job_id": "..." }            (returns immediately)
    GET  <status_url>?job_id=<id> -> the RAW Intune remediation run-state fields
                                     (detectionState, remediationState,
                                      lastStateUpdateDateTime, pre/post script output...)

The teams do NOT interpret those states -- WE do. check_status collapses them into our
3-word contract {state: running|done|failed, progress, result} via _derive_intune(),
using the trigger-time `baseline` (conversations.job_baseline) to tell a fresh result
from a stale row left by a previous run.

Until those endpoints exist, JOBS_DUMMY=true makes start_job return a fake id and
check_status simulate progress across polls, so the whole start -> poll -> done flow
runs locally. Flip JOBS_DUMMY=false (and set the *_START_URL / *_STATUS_URL) when ready.
"""
import uuid
from datetime import datetime, timedelta, timezone

import requests

from app.config import AGENT_HTTP_TIMEOUT, JOBS_DUMMY, logger

# Safety margin so tiny clock differences between our host and Intune can't make us
# reject our own fresh result as "stale". 2 min is well under a job's 3-15 min runtime.
_BASELINE_SKEW = timedelta(seconds=120)

# Detection is still in flight for these -> the run isn't done reporting yet.
_TRANSIENT_DETECTION = (None, "", "unknown", "pending")

# Map any terminal word an app might send to our 3-word contract; unknown -> running.
_STATE_MAP = {
    "done": "done", "success": "done", "completed": "done", "skipped": "done",
    "failed": "failed", "error": "failed", "timed_out": "failed", "not_applicable": "failed",
}


def job_baseline_stamp() -> str:
    """UTC timestamp captured at trigger; a device report NEWER than this is OUR run."""
    return (datetime.now(timezone.utc) - _BASELINE_SKEW).isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _newer(last_updated: str, baseline: str) -> bool:
    """True if the run-state row is newer than the baseline (i.e. it's OUR result)."""
    if not baseline:
        return True
    if not last_updated:
        return False
    try:
        return _parse(last_updated) > _parse(baseline)
    except ValueError:
        return last_updated > baseline  # ISO-8601 UTC sorts lexically anyway


def _derive_intune(data: dict, baseline: str) -> dict:
    """Collapse RAW Intune run-state fields into {state, progress, result}.

    Assumes the run-state fields are at the TOP LEVEL of the JSON. If the teams nest
    them (e.g. under data["value"][0]), unwrap here before reading the fields.

    Two steps: (1) the gate -- is a FRESH result from our trigger back yet? -- and only
    then (2) the outcome -- did the detection/remediation scripts succeed?
    """
    detection = data.get("detectionState")
    remediation = data.get("remediationState")
    last_updated = data.get("lastStateUpdateDateTime")

    # (1) Gate: no fresh row yet becuase job already triggered on start_job so we can wait, and detection still running -> keep polling.
    if not _newer(last_updated, baseline) or detection in _TRANSIENT_DETECTION:
        return {"state": "running",
                "progress": f"Working... ({detection or 'queued'})", "result": None}

    # (2) Outcome — row is fresh and detection is terminal
    # (detection ∈ {success, fail, scriptError, notApplicable}).

    # Hard failures: a script crashed, or remediation ran but didn't resolve it.
    if detection == "scriptError" or remediation in ("remediationFailed", "scriptError"):
        return {"state": "failed", "progress": "Script error on the device",
                "result": data.get("remediationScriptError")
                          or data.get("preRemediationDetectionScriptError")}
    if detection == "notApplicable":
        return {"state": "failed",
                "progress": "This fix doesn't apply to your device", "result": None}

    # Success #1: remediation actually fixed it (takes priority -- true even if the
    # pre-remediation detectionState still reads 'fail').
    if remediation == "success":
        return {"state": "done", "progress": "Complete. Issue remediated.",
                "result": data.get("postRemediationDetectionScriptOutput")
                          or data.get("preRemediationDetectionScriptOutput") or "Fixed."}

    # Success #2: detection found nothing wrong (healthy). Covers 'skipped' remediation state and
    # detect-only policies where remediation is left 'unknown'.
    if detection == "success":
        return {"state": "done", "progress": "Complete. No issues found.",
                "result": data.get("preRemediationDetectionScriptOutput")
                          or "No issues found."}

    # Everything left = an issue was FOUND but NOT fixed (detect-only, skipped, or an
    # unrecognised/unknownFutureValue state). Never say "Complete" -- surface for a human.
    logger.warning("Unresolved/unknown remediation states: detection=%s remediation=%s",
                   detection, remediation)
    return {"state": "failed",
            "progress": "We found an issue but couldn't fix it automatically.",
            "result": data.get("preRemediationDetectionScriptOutput")}

# Fake progress used ONLY in dummy mode so you can watch the polls tick. Kept short (3
# stages) so the dummy reaches "done" within the JOB_MAX_POLLS (5) cap during local tests.
_DUMMY_STAGES = [
    "Checking Outlook profile",
    "Testing mailbox connectivity",
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


def check_status(kind: str, status_url: str, job_id: str, tries: int,
                 baseline: str = "") -> dict:
    """Ask the agent how the job is going. Returns {state, progress, result}.

    `tries` is how many times we've already checked -- used only by the dummy to
    advance through the fake stages so the loop eventually completes. `baseline` is
    the trigger-time timestamp (from conversations.job_baseline) used to tell OUR
    fresh result apart from a stale run-state row left by an earlier run.
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

    # The teams pass through RAW Intune run-state fields -> we interpret them.
    if "detectionState" in data or "remediationState" in data:
        return _derive_intune(data, baseline)

    # Fallback: an app that already returns a normalised state (or a different shape).
    raw = (data.get("state") or data.get("status") or "running").lower()
    return {
        "state": _STATE_MAP.get(raw, "running"),
        "progress": data.get("progress") or data.get("message") or "Working...",
        "result": data.get("result"),
    }
