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
"""
import requests
from requests.adapters import HTTPAdapter


class HttpClient:
    """Owns one pooled requests.Session and the shared timeout."""

    def __init__(self, timeout: int, pool_maxsize: int = 50, session=None):
        self._timeout = timeout
        self._session = session if session is not None else self._build_session(
            pool_maxsize
        )

    # For outbound HTTP using requests.Session, the first call to each agent host
    # does a TCP+TLS handshake; every later call to that same host reuses the open
    # connection, for any user, until it goes idle or the process restarts.
    @staticmethod
    def _build_session(pool_maxsize: int) -> requests.Session:
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
        return self._session.post(url, json=payload, timeout=self._timeout)

    def get(self, url: str, params: dict) -> requests.Response:
        """GET with query params. Raises requests.RequestException on failure."""
        return self._session.get(url, params=params, timeout=self._timeout)

    def close(self) -> None:
        """Release the pooled sockets (process shutdown / tests)."""
        self._session.close()
