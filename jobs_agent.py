"""Client for the diagnostic/troubleshoot Function Apps' ASYNC contract.

Those apps are owned by other teams and expose:
    POST <start_url>              -> { "job_id": "..." }            (returns immediately)
    GET  <status_url>?job_id=<id> -> the RAW Intune remediation run-state fields
                                     (detectionState, remediationState,
                                      lastStateUpdateDateTime, pre/post script output...)

The teams do NOT interpret those states -- WE do. check_status collapses them into
{state, message, output} via _derive_intune(), using the trigger-time `baseline`
(conversations.job_baseline) to tell a fresh result from a stale row left by a previous
run. Three fields, one job each:

    state    ->  running | done | failed. For CODE to branch on.
    message  ->  one line safe to show the user, whatever the state. Our own wording,
                 or the agent's `userMessage` field if it ever provides one.
    output   ->  raw stdout/stderr from the PowerShell script on the user's device.
                 Stored in conversations.job_output for support and NEVER returned to
                 the browser.

`message` vs `output` is a SECURITY boundary, not a naming preference.

The scripts belong to another team and can change without our code changing, so their
output cannot be treated as display text. Real remediation output routinely contains
profile paths (C:\\Users\\jdoe\\...\\jdoe@company.com.ost), registry values, mailbox
addresses and internal server names -- and anything we show is also written into the
transcript, so it would be replayed on every history load.

NO SIMULATION MODE. This client used to fall back to fake progress when the URLs were
unset, which meant a missing App Setting made the app tell real users "No issues found;
Outlook profile repaired" while nothing had run -- and if they then confirmed, the
ServiceNow incident was resolved with the problem untouched. A silent lie is worse than
a visible failure, so an unconfigured URL now raises UpstreamUnavailable. Tests inject a
scripted stand-in for this class instead.
"""
from datetime import datetime, timedelta, timezone

import requests

from app.config import logger
from app.errors import UpstreamTransient, UpstreamUnavailable

from app.clients.http import HttpClient


class JobsClient(HttpClient):
    """Starts long-running device jobs and interprets their raw Intune run-state."""

    # No __init__ of its own: HttpClient already takes and stores `trace`, and
    # check_status reads self._trace from there to discard a "still running" row (a job
    # is polled every 30s for 3-15 minutes, so ~30 of its 31 rows say nothing but "still
    # working"). None means nothing is being traced -- the tests, and any caller that
    # does not want rows.

    # Safety margin so tiny clock differences between our host and Intune can't make
    # us reject our own fresh result as "stale". 2 min is well under a 3-15 min run.
    _BASELINE_SKEW = timedelta(seconds=120)

    # Detection is still in flight for these -> the run isn't done reporting yet.
    _TRANSIENT_DETECTION = (None, "", "unknown", "pending")

    # Map any terminal word an app might send to our 3-word contract; unknown ->
    # running.
    _STATE_MAP = {
        "done": "done", "success": "done", "completed": "done", "skipped": "done",
        "failed": "failed", "error": "failed", "timed_out": "failed",
        "not_applicable": "failed",
    }

    # -- trigger ------------------------------------------------------------
    def baseline_stamp(self) -> str:
        """UTC timestamp captured at trigger; a device report NEWER than this is OUR
        run."""
        return (datetime.now(timezone.utc) - self._BASELINE_SKEW).isoformat()

    def start(self, kind: str, start_url: str, conv_id: str, payload: str) -> str:
        """Kick off the long-running run and return its job_id (does NOT wait).

        Raises rather than inventing a job_id when the URL is unset: a fake id would
        make the flow report a device repair that never happened.
        """
        if not start_url:
            logger.error("no start URL configured for %s jobs", kind)
            raise UpstreamUnavailable(f"{kind} start URL not configured")

        try:
            resp = self.post(start_url, {"conversation_id": conv_id, "input": payload})
            resp.raise_for_status()
            return resp.json()["job_id"]
        except requests.RequestException as exc:
            raise UpstreamTransient(f"{kind} start failed: {exc}")
        except (KeyError, TypeError) as exc:
            # The app answered but without a job_id -- a contract violation, not a blip.
            raise UpstreamUnavailable(f"{kind} start returned no job_id: {exc}")

    # -- status -------------------------------------------------------------
    def check_status(
        self, kind: str, status_url: str, job_id: str, baseline: str = ""
    ) -> dict:
        """Ask the agent how the job is going. Returns {state, message, output}.

        `baseline` is the trigger-time timestamp (from conversations.job_baseline), used
        to tell OUR fresh result apart from a stale run-state row left by an earlier run
        on the same device.

        (A `tries` argument used to be threaded through here for the simulated job mode.
        Both are gone: expiry is wall-clock now, and the stand-in lives in the tests.)
        """
        if not status_url:
            logger.error("no status URL configured for %s jobs", kind)
            raise UpstreamUnavailable(f"{kind} status URL not configured")

        try:
            resp = self.get(status_url, {"job_id": job_id})
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            # Transport-level blip. Raised as the TRANSIENT subclass so the caller knows
            # a retry is worth it -- unlike the missing-URL case above.
            raise UpstreamTransient(f"{kind} status check failed: {exc}")

        # The teams pass through RAW Intune run-state fields -> we interpret them.
        if "detectionState" in data or "remediationState" in data:
            return self._traced(self._derive_intune(data, baseline))

        # Fallback: an app that already returns a normalised state (or another shape).
        # Its `result` is treated as raw output too -- we don't know how it was written,
        # so it is safe-by-default: shown only if the app also sends `userMessage`.
        raw = (data.get("state") or data.get("status") or "running").lower()
        state = self._STATE_MAP.get(raw, "running")
        return self._traced({
            "state": state,
            "message": (
                data.get("userMessage") or data.get("progress")
                or data.get("message") or "Working..."
            ),
            "output": data.get("result"),
        })

    def _traced(self, status: dict) -> dict:
        """Keep the trace row only when the poll carries an outcome.

        TracingSession already recorded the HTTP call by the time we get here, so a
        "still running" answer is discarded again -- otherwise the ~30 no-news polls of
        every job would be the large majority of the agent_calls table. A failed poll is
        never dropped: the row was written with is_error=1 by the transport layer, which
        never reaches this method.
        """
        if self._trace is not None and status.get("state") == "running":
            self._trace.drop_last()
        return status

    # -- Intune interpretation (pure logic, kept with the client that needs it) ---
    @staticmethod
    def _parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    @classmethod
    def _newer(cls, last_updated: str, baseline: str) -> bool:
        """True if the run-state row is newer than the baseline (i.e. it's OUR
        result)."""
        if not baseline:
            return True
        if not last_updated:
            return False
        try:
            return cls._parse(last_updated) > cls._parse(baseline)
        except ValueError:
            return last_updated > baseline  # ISO-8601 UTC sorts lexically anyway

    @classmethod
    def _derive_intune(cls, data: dict, baseline: str) -> dict:
        """Collapse RAW Intune run-state fields into {state, message, output}.

        Assumes the run-state fields are at the TOP LEVEL of the JSON. If the teams
        nest them (e.g. under data["value"][0]), unwrap here before reading the fields.

        Two steps: (1) the gate -- is a FRESH result from our trigger back yet? -- and
        only then (2) the outcome -- did the detection/remediation scripts succeed?
        """
        detection = data.get("detectionState")
        remediation = data.get("remediationState")
        last_updated = data.get("lastStateUpdateDateTime")
        # Everything the device printed, kept together. Goes to conversations.job_output
        # for support; never to the browser.
        raw_output = (
            data.get("postRemediationDetectionScriptOutput")
            or data.get("preRemediationDetectionScriptOutput")
            or data.get("remediationScriptError")
            or data.get("preRemediationDetectionScriptError")
        )
        # If the agent ever adds a field written FOR the end user, prefer it over our
        # generic wording (see the contract note in the module docstring).
        user_message = data.get("userMessage")

        # (1) Gate: no fresh row yet (the job was already triggered at start, so we can
        # wait), or detection still running -> keep polling.
        if not cls._newer(last_updated, baseline) or detection in cls._TRANSIENT_DETECTION:
            return {
                "state": "running",
                "message": f"Working... ({detection or 'queued'})",
                "output": None,
            }

        # (2) Outcome -- row is fresh and detection is terminal
        # (detection in {success, fail, scriptError, notApplicable}).

        # Hard failures: a script crashed, or remediation ran but didn't resolve it.
        # The script's own error text is the most likely place to carry internal paths
        # and server names, so it goes to `output`, never to `result`.
        if detection == "scriptError" or remediation in (
            "remediationFailed", "scriptError"
        ):
            return {
                "state": "failed", "message": "Script error on the device",
                "output": raw_output,
            }
        if detection == "notApplicable":
            return {
                "state": "failed",
                "message": "This fix doesn't apply to your device",
                "output": raw_output,
            }

        # Success #1: remediation actually fixed it (takes priority -- true even if the
        # pre-remediation detectionState still reads 'fail').
        if remediation == "success":
            return {
                "state": "done",
                "message": user_message or "The issue was fixed on your device.",
                "output": raw_output,
            }

        # Success #2: detection found nothing wrong (healthy). Covers 'skipped'
        # remediation state and detect-only policies where remediation is 'unknown'.
        if detection == "success":
            return {
                "state": "done",
                "message": user_message or "No issues were found on your device.",
                "output": raw_output,
            }

        # Everything left = an issue was FOUND but NOT fixed (detect-only, skipped, or
        # an unrecognised/unknownFutureValue state). Never say "Complete" -- surface it.
        logger.warning(
            "Unresolved/unknown remediation states: detection=%s remediation=%s",
            detection, remediation,
        )
        return {
            "state": "failed",
            "message": "We found an issue but couldn't fix it automatically.",
            "output": raw_output,
        }
