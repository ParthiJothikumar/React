"""Base class for the outbound HTTP clients.

Its whole reason to exist is the connection pool. A `requests.Session` keeps sockets
alive between calls; calling `requests.post()` directly (as this app used to) opens a
fresh TCP+TLS connection every time and burns one of the Function App instance's ~128
SNAT ports, which shows up under load as random connection timeouts to our own agent
apps. The Session has to outlive a single call, so it needs an owner -- that owner is
this class, instantiated once per process in deps.py.

Subclasses (AgentClient, MultilingualClient, JobsClient) add the request shapes; this
class only knows how to send and how to pool. `session` is injectable so a test can
pass a stub instead of reaching the network.

It also owns the TIMING of each call, via the optional `trace` (see tracing.py). That
belongs here rather than in a wrapper around the client methods, for two reasons. It is
the same method that already applies the timeout, so the stopwatch cannot drift away from
the call it measures. And it sits BELOW the client methods that deliberately swallow
their own failures -- MultilingualClient.detect returns the previous language,
ServiceNowClient.update_incident logs and returns, call_second_classification returns
{"success": false} -- so a timer placed above any of those would record success on a
timeout.
"""
import time

import requests
from requests.adapters import HTTPAdapter

from app.config import logger


class HttpClient:
    """Owns one pooled requests.Session, the shared timeout, and the call trace."""

    def __init__(self, timeout: int, pool_maxsize: int = 50, session=None, trace=None):
        self._timeout = timeout
        self._session = session if session is not None else self.build_session(
            pool_maxsize
        )
        # The request's CallTrace, or None. None on the process-wide singletons (they
        # exist to hold the pools) and in tests -- _record then does nothing.
        self._trace = trace

    # For outbound HTTP using requests.Session, the first call to each agent host
    # does a TCP+TLS handshake; every later call to that same host reuses the open
    # connection, for any user, until it goes idle or the process restarts.
    @staticmethod
    def build_session(pool_maxsize: int) -> requests.Session:
        """A Session with a sized connection pool mounted for http and https."""
        session = requests.Session()
        #pool_maxsize ->	how many connections to keep alive per host
        #pool_connections ->	how many different hosts to keep a pool for
        adapter = HTTPAdapter(
            pool_connections=pool_maxsize, pool_maxsize=pool_maxsize
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def post(self, url: str, payload: dict) -> requests.Response:
        """POST JSON. Raises requests.RequestException on a transport failure."""
        t0 = time.perf_counter()
        try:
            response = self._session.post(url, json=payload, timeout=self._timeout)
        except Exception as exc:
            self._record(url, payload, None, t0, exc)
            raise  # unchanged: the caller's own error handling still sees the original
        self._record(url, payload, response, t0, None)
        return response

    def get(self, url: str, params: dict) -> requests.Response:
        """GET with query params. Raises requests.RequestException on failure."""
        t0 = time.perf_counter()
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except Exception as exc:
            self._record(url, params, None, t0, exc)
            raise
        self._record(url, params, response, t0, None)
        return response

    def _record(self, url, payload, response, t0, exc) -> None:
        """Add one row to the request's trace. No-op when nothing is tracing.

        perf_counter is monotonic, so an NTP adjustment mid-call cannot produce a
        negative duration, and it is read immediately after the call returns -- our own
        JSON parsing and DB writes stay out of the number, which is what makes it
        honestly "how long the Function App took".

        Best-effort: this runs on the live request path, so a failure while recording
        must never turn a healthy agent call into a failed one.
        """
        if self._trace is None:
            return
        try:
            self._trace.add(
                url,
                payload,
                response,
                int((time.perf_counter() - t0) * 1000),
                f"{type(exc).__name__}: {exc}" if exc is not None else None,
            )
        except Exception:
            logger.exception("could not record an agent call (row dropped)")

    def close(self) -> None:
        """Release the pooled sockets (process shutdown / tests)."""
        self._session.close()
