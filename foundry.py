"""Azure AI Foundry client (lazy, thread-safe, one per worker process).

Azure Functions does not run FastAPI's lifespan, so the Foundry client cannot be built
at startup -- it is created on the FIRST request and reused for the life of the worker
process. That used to be a module-level `state` dict plus a `_state_ready` flag and a
`global` statement, i.e. an object written without a class: two threads could enter it
at once and each build a client, and a failed init left the flag set.

Wrapping it in a class fixes both: the Lock makes the build happen exactly once, and
the built client is only stored after a successful build, so a failed init retries on
the next request instead of caching a half-built client.
"""
import threading

from app.config import logger
from app.errors import UpstreamUnavailable


class FoundryClient:
    """Lazily builds and caches the AIProjectClient for this worker process."""

    def __init__(self, endpoint: str, timeout: int = 20, max_retries: int = 1):
        self._endpoint = endpoint
        # Bounds ONE request; see Settings.FOUNDRY_HTTP_TIMEOUT for the worst-case math.
        self._timeout = timeout
        self._max_retries = max_retries
        # Both are filled in by _ensure() on first use -- nothing is built here, so
        # constructing this object costs nothing and touches no network.
        #   _openai_client MUST start as None: _ensure() reads it as its
        #                  "have I built this already?" check.
        #   _project_client is never read; it is held so the AIProjectClient that
        #                  produced the OpenAI client stays referenced for the life of
        #                  the process rather than becoming garbage.
        self._project_client = None
        self._openai_client = None
        self._lock = threading.Lock()

    @property
    def openai(self):
        """The OpenAI-compatible client, building it on first use."""
        self._ensure()
        return self._openai_client

    def _ensure(self) -> None:
        """Build the client once per process, under a lock.

        Double-checked locking: the fast path is a plain attribute read with no lock,
        and only the first callers serialise. `_openai_client` is assigned last, so a
        failed build leaves the object un-initialised and the next request retries
        instead of finding a half-built client cached.
        """
        #A process starts. The first request in builds the Foundry client while a couple of others briefly wait on the lock; every request after that reuses it with no waiting
        if self._openai_client is not None:
            return
        with self._lock:
            if self._openai_client is not None:
                return
            # Imported here rather than at module scope so importing this module (and
            # therefore the app) doesn't require the Azure SDKs to be installed.
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            project_client = AIProjectClient(
                credential=DefaultAzureCredential(),
                endpoint=self._endpoint,
                # azure-core transport options, for AIProjectClient's own calls.
                connection_timeout=self._timeout,
                read_timeout=self._timeout,
            )
            # get_openai_client() passes its keyword arguments straight to the OpenAI
            # client constructor, so this is what actually bounds conversations.create()
            # -- the first network call of every new chat. max_retries is set explicitly
            # because the SDK defaults to 2, which would silently triple the ceiling.
            openai_client = project_client.get_openai_client(
                timeout=self._timeout, max_retries=self._max_retries
            )
            self._project_client = project_client
            self._openai_client = openai_client
            logger.info(
                "Foundry client initialized (lazy) timeout=%ss max_retries=%s",
                self._timeout, self._max_retries,
            )

    def create_conversation(self):
        """Create a new, empty Foundry conversation.

        Every failure below this line becomes UpstreamUnavailable, so this client raises
        the same domain error as AgentClient instead of leaking SDK types (openai's
        APITimeoutError, azure-core's ServiceRequestError, azure-identity's
        ClientAuthenticationError). Two reasons that matters:

          * the log line names the cause -- callers used to see only a generic
            "chat failed" with a raw traceback;
          * nothing above this layer has to import openai or azure.core to reason about
            a failure, and a non-HTTP caller (the job reaper, a script) gets an error it
            can recognise.

        The chat controller still turns this into the user-facing fallback bubble; it
        catches Exception, and UpstreamUnavailable is one.
        """
        try:
            return self.openai.conversations.create()
        except Exception as exc:
            # Includes the lazy build in _ensure(), which runs on the first call.
            logger.error(
                "foundry create_conversation failed (%s): %s", type(exc).__name__, exc
            )
            raise UpstreamUnavailable("Foundry conversation could not be created")
