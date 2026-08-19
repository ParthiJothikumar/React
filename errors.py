"""Domain errors -- the framework-free vocabulary the layers below HTTP raise.

flow.py, services.py, persistence.py and clients/ must not know that HTTP exists, so
they raise these instead of fastapi.HTTPException. routes.py (plus the handlers in
main.py) is the ONE place that decides which status code each one becomes.

That is what keeps those layers callable from somewhere other than a web request --
a timer trigger, a CLI script, a unit test -- with no FastAPI involved.
"""


class AppError(Exception):
    """Base class for every domain error raised below the HTTP layer."""


class NotFound(AppError):
    """A session/conversation does not exist for this user. -> 404"""


class ConversationEnded(AppError):
    """The conversation is terminal (stage=DONE); a new one must be started. -> 409"""


class TurnFailed(AppError):
    """A turn failed, but the CONVERSATION is intact and the user can retry.

    Raised only by ChatService.continue_chat, wrapping whatever went wrong. It carries
    the stage the conversation is ACTUALLY still at, because the HTTP layer cannot know
    it -- the failure happened before a stage could be returned.

    Why that matters: without it the response said done=True / stage=DONE, so the
    frontend reset a perfectly good conversation over one transient agent timeout. The
    user then re-described the problem from scratch and we opened a SECOND ServiceNow
    incident. With the real stage and done=False they simply resend the same message.

    `incident_id` is set when a ServiceNow ticket was already raised before the failure.
    That changes what we tell the user: instead of "please try again" -- which makes them
    start over and open a SECOND ticket -- we hand them the number that already exists
    and tell them not to. Removing the reason to retry is what prevents the duplicate.

    The original cause is preserved on __cause__ (raised with `from`), so the log line
    still shows what actually broke.
    """

    def __init__(self, stage, conversation_id, cause: Exception, incident_id=None):
        super().__init__(str(cause))
        self.stage = stage
        self.conversation_id = conversation_id
        self.incident_id = incident_id


class UpstreamUnavailable(AppError):
    """An agent Function App we call out to failed or isn't configured. -> 502"""


class UpstreamTransient(UpstreamUnavailable):
    """A momentary failure calling an agent app -- worth retrying. -> 502

    Subclasses UpstreamUnavailable so it still maps to 502 without its own handler
    (Starlette matches the closest registered class). The distinction exists for
    RETRY decisions, not for the status code: JobService retries this one and lets a
    plain UpstreamUnavailable through, because a missing URL will never succeed on the
    second attempt.
    """


class DatabaseUnavailable(AppError):
    """The state store is unreachable or not configured. -> 503"""
