"""HTTP-triggered function: take a message + conv_id, return the agent's reply.

Request (JSON body or query string):
    { "message": "...", "conv_id": "conv_..." }   # conv_id optional

Response (JSON):
    { "conv_id": "conv_...", "reply": "..." }
"""

import os
import json
import logging

import azure.functions as func
from azure.identity import ClientSecretCredential
from azure.ai.projects import AIProjectClient

# Build the client once per worker (reused across invocations)
_credential = ClientSecretCredential(
    tenant_id=os.environ["AZURE_SP_TENANT_ID"],
    client_id=os.environ["AZURE_SP_CLIENT_ID"],
    client_secret=os.environ["AZURE_SP_CLIENT_SECRET"],
)
_project_client = AIProjectClient(
    credential=_credential,
    endpoint=os.environ["AZURE_FOUNDRY_PROJECT_ENDPOINT"],
)
_client = _project_client.get_openai_client()
_agent_name = os.environ["AGENT_NAME"]


def _json(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status,
        mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    # Accept params from JSON body or query string
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    message = body.get("message") or req.params.get("message")
    conv_id = body.get("conv_id") or req.params.get("conv_id")

    if not message:
        return _json({"error": "message is required"}, status=400)

    try:
        # Reuse the conversation if given, otherwise start a new one
        if not conv_id:
            conv_id = _client.conversations.create().id

        resp = _client.responses.create(
            input=message,
            conversation=conv_id,
            extra_body={"agent": {"type": "agent_reference", "name": _agent_name}},
        )

        return _json({"conv_id": conv_id, "reply": (resp.output_text or "").strip()})

    except Exception as e:
        logging.exception("agent call failed")
        return _json({"error": f"{type(e).__name__}: {e}"}, status=500)
