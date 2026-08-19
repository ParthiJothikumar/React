"""FastAPI application factory, CORS middleware, and domain-error mapping.

Exposes `app`, the FastAPI instance the Functions host (http_app/) mounts under
/workflow via func.AsgiMiddleware. This module only wires the app together -- config
lives in config.py, endpoints in routes.py, business logic in services.py + flow.py.
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import logger, settings
from app.errors import (
    AppError,
    ConversationEnded,
    DatabaseUnavailable,
    NotFound,
    UpstreamUnavailable,
)
from app.routes import router

app = FastAPI(title="IT Support Orchestrator API")

# One startup check: log loudly about settings that are fine locally but wrong in
# production (JOBS_DUMMY left on, SQLite in use, missing agent URLs).
settings.warn_on_risky_config()

# Credentials require explicit origins (never "*") per browser rules and Snyk; if
# CORS_ORIGINS is ever set to "*", drop credentials rather than ship the invalid,
# insecure "*" + credentials combination.
_allow_credentials = "*" not in settings.CORS_ORIGINS
if not _allow_credentials:
    logger.warning("CORS_ORIGINS contains '*'; disabling allow_credentials")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Domain error -> HTTP status. The ONE place that translation happens, so the
# layers below (services.py, flow.py, repositories.py, clients/) can raise the
# framework-free errors in errors.py and stay callable without a web server.
#
# The detail text of NotFound/ConversationEnded is author-written and safe to
# show; DatabaseUnavailable/UpstreamUnavailable carry internal causes, so those
# are logged and replaced with a generic line.
#
# A handler only fires if nothing caught the exception on the way up -- the
# endpoints in routes.py deliberately re-raise NotFound/ConversationEnded so they
# reach here, and swallow everything else into a user-facing fallback.
#
# Starlette matches the CLOSEST registered class, walking up the hierarchy. So the
# four specific handlers below win over the AppError catch-all at the end.
# ---------------------------------------------------------------------------
@app.exception_handler(NotFound)
async def _handle_not_found(request: Request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ConversationEnded)
async def _handle_conversation_ended(request: Request, exc: ConversationEnded):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(UpstreamUnavailable)
async def _handle_upstream_unavailable(request: Request, exc: UpstreamUnavailable):
    logger.error("upstream unavailable path=%s: %s", request.url.path, exc)
    return JSONResponse(status_code=502, content={"detail": "Agent call failed"})


@app.exception_handler(DatabaseUnavailable)
async def _handle_database_unavailable(request: Request, exc: DatabaseUnavailable):
    logger.error("database unavailable path=%s: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


@app.exception_handler(AppError)
async def _handle_unmapped_domain_error(request: Request, exc: AppError):
    """Last resort: a domain error with no handler of its own.

    The four handlers above take priority, so reaching here means a subclass was added
    to errors.py and never mapped. The status stays 500 because we genuinely don't know
    what an unmapped error means -- inventing a 4xx would be worse. The value is the log
    line: it names the type, instead of leaving an anonymous 500 to be guessed at.
    """
    logger.error(
        "unmapped domain error %s on %s: %s",
        type(exc).__name__, request.url.path, exc,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


app.include_router(router)
