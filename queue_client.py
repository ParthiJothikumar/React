"""Queue helper: put a job message on the jobs queue (the enqueue side).

The flow enqueues the FIRST message when a job starts; the worker re-enqueues the
SAME message with a delay to poll again. Both go through here. Messages are plain
JSON text (host.json sets queues.messageEncoding = "none", so no base64).

This is the ONLY place we talk to the Storage Queue, so swapping transports later
(e.g. a plain-FastAPI consumer on Container Apps) touches just this module.
"""
import json

from azure.storage.queue import QueueClient

from app.config import JOBS_QUEUE_CONNECTION, JOBS_QUEUE_NAME, logger

_client = None


def _get_client() -> QueueClient:
    """Lazily build (and cache) the QueueClient; create the queue if missing."""
    global _client
    if _client is None:
        if not JOBS_QUEUE_CONNECTION:
            raise RuntimeError("AzureWebJobsStorage not set -- cannot reach the jobs queue")
        _client = QueueClient.from_connection_string(
            JOBS_QUEUE_CONNECTION, JOBS_QUEUE_NAME
        )
        try:
            _client.create_queue()
        except Exception:
            pass  # already exists -> fine
    return _client


def enqueue(message: dict, delay_seconds: int = 0) -> None:
    """Send a job message; make it invisible for `delay_seconds` (the poll delay).

    delay_seconds=0  -> processed right away (the first message).
    delay_seconds=30 -> processed 30s later (a re-check). This delay is what replaces
    a timer: the delayed message IS the "poll every N seconds".
    """
    client = _get_client()
    client.send_message(json.dumps(message), visibility_timeout=delay_seconds)
    logger.info(
        "enqueued job message conv_id=%s delay=%ss",
        message.get("conversation_id"), delay_seconds,
    )
