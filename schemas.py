"""Request and response models (Pydantic).

The response models are a security control, not documentation. FastAPI serialises the
return value THROUGH the declared model and drops anything not listed, so a field that
should never reach the browser cannot leak by accident -- e.g. if `flow_vars` (which
holds last_error, kb_id and incident_id) were ever added to a payload dict, it would
be stripped here rather than shipped.

Keep every field a controller returns declared below, including the error fields on the
fallback path: once response_model is set, a MISSING required field becomes a 500.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    # Bounded because the column is NVARCHAR(MAX) but user_id is NVARCHAR(200), and an
    # unbounded message is forwarded verbatim to the agents.
    message: str = Field(min_length=1, max_length=8000)


class ContinueChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    session_id: Optional[str] = Field(default=None, max_length=200)
    # Accepted for backwards compatibility with older frontend builds; the service
    # resolves the conversation from session_id only.
    conversation_id: Optional[str] = Field(default=None, max_length=200)


class ChatResponse(BaseModel):
    """What /chat, /chat/continue and /jobs/status return."""

    user_id: str
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    stage: Optional[str] = None
    done: bool = False
    messages: List[str] = []
    answer: str = ""
    # Set only on the fallback path, so the FE can render an error bubble.
    error: bool = False


class SessionSummary(BaseModel):
    """One row of the frontend's left panel. No message content."""

    id: Optional[str] = None
    session_id: Optional[str] = None
    current_conversation_id: Optional[str] = None
    title: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SessionListResponse(BaseModel):
    sessions: List[SessionSummary] = []


class TranscriptTurn(BaseModel):
    role: str
    content: str


class TranscriptResponse(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    conversations: List[TranscriptTurn] = []
