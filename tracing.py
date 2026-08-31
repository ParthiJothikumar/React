"""The rows collected for the agent_calls table during one request.

This module holds only the container and the URL -> name map. The measuring happens in
HttpClient.post/get (see clients/http.py), which is the one place every agent call passes
through -- so no `trace` argument has to be threaded through step(), six stage handlers
and eight client methods.

A CallTrace is created per request in deps.py and handed to that request's clients. It is
never shared: request 1's rows must not land in request 2's list. The pooled sessions
stay process-wide and are passed in alongside it, so nothing about connection reuse
changes.
"""
import json

MAX_FIELD = 8000  # cap per column: one runaway script must not bloat the table


class CallTrace:
    """The rows collected during ONE request. deps.py builds a fresh one per request."""

    def __init__(self, agent_names: dict):
        self._names = agent_names  # {url without query string: label}
        self.rows = []

    def add(self, url, payload, response, duration_ms, error=None):
        # A 4xx/5xx raises nothing here (raise_for_status is the caller's job, and some
        # callers never call it) but it is still a failed call.
        if error is None and response is not None and not response.ok:
            error = f"HTTP {response.status_code}"
        self.rows.append(
            {
                # Query string stripped: function keys travel in ?code=...
                "agent": self._names.get(str(url).split("?")[0], "unknown"),
                "request_text": _field(payload),
                "response_json": _field(response.text if response is not None else None),
                "duration_ms": duration_ms,
                "is_error": bool(error),
                "error_text": _field(error),
            }
        )

    def drop_last(self):
        """Drop the row just added -- used by the status poll.

        A job runs 3-15 minutes and is polled every 30s, so ~30 of its 31 rows would say
        "still running". Only the final poll carries the outcome.

        NOTE for anyone adding a verb to HttpClient: put/delete/patch need the same
        timing block post() and get() have, or calls made with them are silently absent
        from the table -- no error, no log line.
        """
        if self.rows:
            self.rows.pop()


def agent_names(settings) -> dict:
    """URL -> agent label, built once at startup.

    A job agent's start and status URLs map to the same label: it is the same Function
    App, and which endpoint was called is already clear from the row's request body.
    """
    pairs = (
        (settings.ORCHESTRATOR_AGENT, "orchestrator"),
        (settings.FIRST_CLASSIFICATION_AGENT, "classify_1"),
        (settings.SECOND_CLASSIFICATION_AGENT, "classify_2"),
        (settings.DIAGNOSTICS_START_URL, "diagnostics"),
        (settings.DIAGNOSTICS_STATUS_URL, "diagnostics"),
        (settings.TROUBLESHOOT_START_URL, "troubleshoot"),
        (settings.TROUBLESHOOT_STATUS_URL, "troubleshoot"),
        (settings.SERVICENOW_AGENT, "servicenow"),
        (settings.MULTILINGUAL_AGENT, "multilingual"),
    )
    return {str(url).split("?")[0]: name for url, name in pairs if url}


def _field(value):
    """Serialize a value for storage and cap its length."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + "...[truncated]"
