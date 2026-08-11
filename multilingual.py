"""Multilingual helpers (detect + translate via the multilingual agent)."""
import requests

from app.config import AGENT_HTTP_TIMEOUT, DEFAULT_LANG, MULTILINGUAL_AGENT, logger


def _post_multilingual(payload: dict) -> dict:
    """POST a structured request to the multilingual Function App, return its JSON.

    Uses its own request shape (not call_agent's {conversation_id, message}) because
    the multilingual agent is stateless -- no conversation id, no shared thread. The
    payload is {"agent": "detect"|"translate", "message": <text>}, plus "lang":
    <target ISO> for translate. Raises on transport error; callers catch.
    """
    resp = requests.post(MULTILINGUAL_AGENT, json=payload, timeout=AGENT_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def detect_language(text: str, fallback: str = "") -> str:
    """Detect the user's language (ISO code) via the multilingual agent, every turn.

    Keeps `fallback` (the previously detected language) -- or DEFAULT_LANG if none --
    when the agent isn't configured, the text is empty, the language is unsupported,
    detection is low-confidence, or the call fails. So a short reply like "ok" never
    breaks the chat or wrongly flips the conversation language.
    """
    fallback_lang = fallback or DEFAULT_LANG
    if not MULTILINGUAL_AGENT or not text or not text.strip():
        return fallback_lang
    try:
        resp = _post_multilingual({"agent": "detect", "message": text})
        if not resp.get("supported"):
            return fallback_lang  # unsupported language -> stay in the default language
        return (resp.get("code") or fallback_lang).strip()
    except Exception:
        logger.exception("detect_language failed")
        return fallback_lang


def translate_messages(messages: list, lang: str) -> list:
    """Translate each outgoing message into `lang` (ISO code) via the multilingual
    agent, right before it goes to the frontend.

    Per-message so the FE can still render each as its own bubble. No-ops (returns
    the originals) when the agent isn't configured or lang == DEFAULT_LANG. A
    per-message failure keeps that message's original text, so a translation outage
    degrades gracefully instead of 502-ing the whole chat.
    """
    if not MULTILINGUAL_AGENT or not lang or lang == DEFAULT_LANG:
        return messages
    out = []
    for msg in messages:
        if not msg or not msg.strip():
            out.append(msg)
            continue
        try:
            resp = _post_multilingual(
                {"agent": "translate", "lang": lang, "message": msg}
            )
            out.append(resp.get("reply") or msg)
        except Exception:
            logger.exception("translate failed")
            out.append(msg)
    return out


def translate_to_english(text: str, lang: str) -> str:
    """Translate an inbound user message into English (DEFAULT_LANG).

    Lets the flow logic (the "yes"/"no" checks) and the sub-agents all operate in
    one language regardless of what the user typed. No-op when the multilingual
    agent isn't configured, the text is empty, or the user is already writing in
    DEFAULT_LANG. Returns the original text on failure so a translation outage
    never blocks the conversation.
    """
    if (
        not MULTILINGUAL_AGENT
        or not text
        or not text.strip()
        or not lang
        or lang == DEFAULT_LANG
    ):
        return text
    try:
        resp = _post_multilingual(
            {"agent": "translate", "lang": DEFAULT_LANG, "message": text}
        )
        return resp.get("reply") or text
    except Exception:
        logger.exception("translate_to_english failed")
        return text
