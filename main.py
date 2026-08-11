"""FastAPI application factory + CORS middleware.

Exposes `app`, the FastAPI instance the Functions host (http_app/) mounts under
/workflow via func.AsgiMiddleware. This module only wires the app together -- config
lives in config.py, endpoints in routes.py.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import CORS_ORIGINS, logger
from app.routes import router

app = FastAPI(title="IT Support Orchestrator API")

# Credentials require explicit origins (never "*") per browser rules and Snyk; if
# CORS_ORIGINS is ever set to "*", drop credentials rather than ship the invalid,
# insecure "*" + credentials combination.
_allow_credentials = "*" not in CORS_ORIGINS
if not _allow_credentials:
    logger.warning("CORS_ORIGINS contains '*'; disabling allow_credentials")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)
