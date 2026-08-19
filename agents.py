"""Client for the sibling agent Function Apps."""
import requests

from app.config import logger
from app.errors import UpstreamUnavailable

from app.clients.http import HttpClient


class AgentClient(HttpClient):
    """POSTs to any agent Function App over the shared pooled connection.

    Each agent is a separate Function App. We send the conversation id (for
    continuity) and the message; the agent runs internally and returns JSON.
    """

    def __init__(
        self,
        timeout: int,
        second_classification_url: str = "",
        pool_maxsize: int = 50,
        session=None,
    ):
        super().__init__(timeout, pool_maxsize=pool_maxsize, session=session)
        # Held on the client because call_second_classification() is the one method
        # bound to a specific agent (it speaks that agent's richer contract).
        self._second_classification_url = second_classification_url

    def call_json(self, conv_id: str, agent_url: str, message: str) -> dict:
        """POST to an agent and return its FULL JSON response dict.

        Raises UpstreamUnavailable if the URL isn't configured or the call fails.

        WHAT THE USER SEES: this client is only called from flow.py, i.e. the chat
        endpoints -- and those catch Exception and return the fallback bubble, so the
        result is HTTP 200 with {"stage": "ERROR", "error": true} and no conversation
        saved. NOT a 502. (A 502 only happens for JobsClient failures, because
        /jobs/status deliberately re-raises UpstreamUnavailable.)
        """
        if not agent_url:
            logger.error("agent url not configured")
            raise UpstreamUnavailable("Agent URL not configured")

        try:
            resp = self.post(
                agent_url,
                {"conversation_id": conv_id, "message": message if message else " "},
            )
            # raise_for_status() inspects the status code and raises
            # requests.HTTPError on 4xx/5xx.
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("agent call failed url=%s: %s", agent_url, exc)
            raise UpstreamUnavailable("Agent call failed")

    def call_structured(self, agent_url: str, payload: dict) -> dict:
        """POST a body whose fields are already separate keys, and return its JSON.

        Unlike call_json, the caller supplies the WHOLE body rather than one message
        string. Use this wherever a value could contain the delimiter of a command
        string -- with real JSON fields, a user's text can hold any characters and
        still cannot become a second instruction. See ServiceNowClient.
        """
        if not agent_url:
            logger.error("agent url not configured")
            raise UpstreamUnavailable("Agent URL not configured")

        try:
            resp = self.post(agent_url, payload)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("agent call failed url=%s: %s", agent_url, exc)
            raise UpstreamUnavailable("Agent call failed")

    def call_text(self, conv_id: str, agent_url: str, message: str) -> str:
        """Return just the human-readable text from the agent's response.

        Reads the first present of 'message' / 'reply' / 'agent_message' (empty string
        if none), for replies shown to the user. Structured agents use call_json.
        """
        return self.reply_text(self.call_json(conv_id, agent_url, message))

    @staticmethod
    def reply_text(resp: dict) -> str:
        """The human-readable line from an agent response, or "" if it carries none."""
        return (
            resp.get("message")
            or resp.get("reply")
            or resp.get("agent_message")
            or ""
        )

    def call_second_classification(self, conv_id, kb_id, message: str = "") -> dict:
        """Ask the SECOND classification agent how to resolve the issue.

        Posts {conversation_id, kb_id, message} ("" on the first call, the user's reply
        on ask_user follow-ups -- the agent owns the follow-up count) and returns its
        ValidationResponse dict. NEVER raises: any transport/HTTP failure becomes a
        {success: false, error: <cause>} dict so the flow router handles every failure
        the same way (show a user-safe line, stash the raw cause).
        """
        if not self._second_classification_url:
            logger.error("second classification agent url not configured")
            return self._failure("Agent URL not configured")

        body = {"conversation_id": conv_id, "kb_id": kb_id, "message": message}
        try:
            resp = self.post(self._second_classification_url, body)
        except requests.RequestException as exc:
            logger.error(
                "second classification call failed conv_id=%s: %s", conv_id, exc
            )
            return self._failure(str(exc))

        try:
            data = resp.json()
        except ValueError:
            data = None
        # Use the agent's own JSON body even on a non-2xx (its HTTP-layer errors carry
        # a {success: false, ...} body); otherwise synthesize a failure from the status.
        if isinstance(data, dict) and (resp.ok or "success" in data or "error" in data):
            return data
        return self._failure(
            f"HTTP {resp.status_code} from second classification agent"
        )

    @staticmethod
    def _failure(error: str) -> dict:
        """Synthesize a ValidationResponse-shaped failure. agent_message is left empty
        so the flow shows its generic fallback; the raw cause goes in `error`
        (logs/DB)."""
        return {
            "success": False,
            "validated": False,
            "action": None,
            "agent_message": "",
            "error": error,
        }
