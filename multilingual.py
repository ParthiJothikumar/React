"""Client for the multilingual agent (detect language + translate)."""
from app.config import logger

from app.clients.http import HttpClient


class MultilingualClient(HttpClient):
    """Detects the user's language and translates messages both ways.

    Uses its own request shape (not AgentClient's {conversation_id, message}) because
    this agent is stateless -- no conversation id, no shared thread. The payload is
    {"agent": "detect"|"translate", "message": <text>}, plus "lang": <target ISO> for
    translate.

    Every method degrades instead of raising: a translation outage must never break a
    conversation, so callers get the original text back.
    """

    def __init__(
        self,
        agent_url: str,
        default_lang: str,
        timeout: int,
        pool_maxsize: int = 50,
        session=None,
    ):
        super().__init__(timeout, pool_maxsize=pool_maxsize, session=session)
        self._agent_url = agent_url
        self._default_lang = default_lang

    @property
    def enabled(self) -> bool:
        """False when no multilingual agent is configured -- every method no-ops."""
        return bool(self._agent_url)

    def _call(self, payload: dict) -> dict:
        """POST a structured request and return its JSON. Raises; callers catch."""
        resp = self.post(self._agent_url, payload)
        resp.raise_for_status()
        return resp.json()

    def detect(self, text: str, fallback: str = "") -> str:
        """Detect the user's language (ISO code), called every turn.

        Keeps `fallback` (the previously detected language) -- or the default language
        if none -- when the agent isn't configured, the text is empty, the language is
        unsupported, detection is low-confidence, or the call fails. So a short reply
        like "ok" never breaks the chat or wrongly flips the conversation language.
        """
        fallback_lang = fallback or self._default_lang
        if not self.enabled or not text or not text.strip():
            return fallback_lang
        try:
            resp = self._call({"agent": "detect", "message": text})
            if not resp.get("supported"):
                return fallback_lang  # unsupported -> stay in the default language
            return (resp.get("code") or fallback_lang).strip()
        except Exception:
            logger.exception("detect_language failed")
            return fallback_lang

    def translate_messages(self, messages: list, lang: str) -> list:
        """Translate each outgoing message into `lang`, right before it goes to the FE.

        Per-message so the FE can still render each as its own bubble. No-ops (returns
        the originals) when the agent isn't configured or lang is the default. A
        per-message failure keeps that message's original text, so a translation outage
        degrades gracefully instead of 502-ing the whole chat.
        """
        if not self.enabled or not lang or lang == self._default_lang:
            return messages
        out = []
        for msg in messages:
            if not msg or not msg.strip():
                out.append(msg)
                continue
            try:
                resp = self._call(
                    {"agent": "translate", "lang": lang, "message": msg}
                )
                out.append(resp.get("reply") or msg)
            except Exception:
                logger.exception("translate failed")
                out.append(msg)
        return out

    def to_english(self, text: str, lang: str) -> str:
        """Translate an inbound user message into the default language.

        Lets the flow logic (the "yes"/"no" checks) and the sub-agents all operate in
        one language regardless of what the user typed. No-op when the agent isn't
        configured, the text is empty, or the user is already writing in the default
        language. Returns the original text on failure so a translation outage never
        blocks the conversation.
        """
        if (
            not self.enabled
            or not text
            or not text.strip()
            or not lang
            or lang == self._default_lang
        ):
            return text
        try:
            resp = self._call(
                {"agent": "translate", "lang": self._default_lang, "message": text}
            )
            return resp.get("reply") or text
        except Exception:
            logger.exception("translate_to_english failed")
            return text
