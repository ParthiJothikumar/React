"""Diagnostics agent as an HTTP API -- the /start + /status contract the orchestrator calls.

WHY THIS EXISTS
The original was a CLI using input(), so it could not be called by anything. This exposes
the same logic as the two endpoints the orchestrator already expects:

    POST /start              -> {"job_id": "..."}   returns immediately, does NOT wait
    GET  /status?job_id=...  -> the RAW Intune run-state fields

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8001
    open http://127.0.0.1:8001/docs        <- try both endpoints from the browser

CONTRACT NOTE -- device_id
The orchestrator currently posts {"conversation_id", "input"} where input is a JSON STRING
of {"kb_id", "summary"}. There is no device_id and no user_id in it, so this service has no
way to know WHICH machine to remediate. Until that is agreed, device_id must be included
in `input`. /start returns a clear 400 when it is absent rather than guessing.

Options to settle with the orchestrator team:
  a) it sends device_id explicitly (simplest)
  b) it sends user_id and this service resolves the device via Graph
"""
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("diagnostics_api")


# ===================================== Config ======================================

@dataclass
class Config:
    # Foundry
    foundry_endpoint: str
    agent_name: str
    agent_version: str
    # Entra app registration used for Graph
    tenant_id: str
    client_id: str
    client_secret: str
    # Local testing without Azure. OFF unless explicitly enabled -- a service that
    # silently pretends to repair machines is worse than one that plainly fails.
    mock: bool

    @property
    def graph_ready(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)


def load_config() -> Config:
    cfg = Config(
        foundry_endpoint=os.getenv("FOUNDRY_ENDPOINT", ""),
        agent_name=os.getenv("AGENT_NAME", "Diagnostics-agent"),
        agent_version=os.getenv("AGENT_VERSION", "3"),
        tenant_id=os.getenv("AZURE_TENANT_ID", ""),
        client_id=os.getenv("AZURE_CLIENT_ID", ""),
        client_secret=os.getenv("AZURE_CLIENT_SECRET", ""),
        mock=os.getenv("DIAGNOSTICS_MOCK", "false").strip().lower() == "true",
    )
    if cfg.mock:
        logger.warning(
            "DIAGNOSTICS_MOCK is ON -- no agent call, no Intune trigger, fabricated "
            "run states. For contract testing only; never set this in a real environment."
        )
    else:
        if not cfg.foundry_endpoint:
            logger.warning("FOUNDRY_ENDPOINT not set -- /start will fail")
        if not cfg.graph_ready:
            logger.warning(
                "AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET not all set "
                "-- Intune calls will fail"
            )
    return cfg


config = load_config()


# ============================== Shared HTTP session ===============================
# One pooled Session for the process. Calling requests.post() directly opens a fresh
# TCP+TLS connection every time and burns one of the host's limited outbound ports --
# which shows up under load as random timeouts to Graph.
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

GRAPH_TIMEOUT = int(os.getenv("GRAPH_TIMEOUT", "30"))


# ================================ Graph auth ======================================

class GraphToken:
    """Caches the client-credentials token instead of fetching one per call.

    The original asked Entra for a new token on every remediation trigger. Tokens are
    valid for ~60 minutes, so that was one wasted round trip per request.
    """

    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        import time

        if self._token and time.time() < self._expires_at - 60:
            return self._token

        if not config.graph_ready:
            raise HTTPException(
                status_code=500,
                detail="Graph credentials not configured (AZURE_TENANT_ID / "
                       "AZURE_CLIENT_ID / AZURE_CLIENT_SECRET)",
            )

        url = (
            f"https://login.microsoftonline.com/{config.tenant_id}"
            "/oauth2/v2.0/token"
        )
        resp = _session.post(
            url,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=GRAPH_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        logger.info("Graph token acquired")
        return self._token


graph_token = GraphToken()


# ============================== Diagnostics agent =================================

class DiagnosticsAgentService:
    """Maps a KB id to an Intune script id, via the Foundry agent.

    The Foundry client is built lazily and cached: creating it does an Entra handshake, so
    doing it at import would make startup fail whenever Azure is briefly unreachable, and
    doing it per request would add that handshake to every call.
    """

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            if not config.foundry_endpoint:
                raise HTTPException(
                    status_code=500, detail="FOUNDRY_ENDPOINT not configured"
                )
            self._client = AIProjectClient(
                endpoint=config.foundry_endpoint,
                credential=DefaultAzureCredential(),
            ).get_openai_client(timeout=60, max_retries=1)
            logger.info("Foundry client initialised")
        return self._client

    def map_kb_to_script(self, device_id: str, kb_id: str) -> dict:
        logger.info("Asking the agent to map KB %s", kb_id)
        response = self.client.responses.create(
            input=[{
                "role": "user",
                "content": f"Target Device ID: {device_id}. Issue KB: {kb_id}",
            }],
            extra_body={
                "agent_reference": {
                    "name": config.agent_name,
                    "version": config.agent_version,
                    "type": "agent_reference",
                }
            },
        )
        raw = (response.output_text or "").strip()
        # Agents often wrap JSON in a markdown fence; strip it before parsing.
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Log the raw text -- without it, "why did mapping fail?" is unanswerable.
            logger.error("Agent did not return JSON: %r", raw[:500])
            raise HTTPException(
                status_code=502,
                detail="Diagnostics agent returned a non-JSON response",
            )


agent_service = DiagnosticsAgentService()


# ============================== Intune remediation ================================

def trigger_remediation(device_id: str, script_policy_id: str) -> None:
    """Fire an on-demand proactive remediation. Raises with the reason on failure.

    The original returned a bool, which threw away Graph's error body -- so a 403 from a
    missing permission and a 404 from a wrong device id looked identical to the caller.
    """
    url = (
        "https://graph.microsoft.com/beta/deviceManagement/managedDevices/"
        f"{device_id}/initiateOnDemandProactiveRemediation"
    )
    resp = _session.post(
        url,
        headers={
            "Authorization": f"Bearer {graph_token.get()}",
            "Content-Type": "application/json",
        },
        json={"scriptPolicyId": script_policy_id},
        timeout=GRAPH_TIMEOUT,
    )
    logger.info("Graph remediation trigger -> %s", resp.status_code)
    if resp.status_code != 204:
        raise HTTPException(
            status_code=502,
            detail=f"Intune rejected the trigger ({resp.status_code}): {resp.text[:300]}",
        )


# Safety valve for the pagination loop below.
MAX_RUN_STATE_PAGES = int(os.getenv("MAX_RUN_STATE_PAGES", "20"))


def fetch_run_state(script_id: str, device_id: str) -> dict:
    """Return the RAW Intune run-state record for one device, or a 'pending' shape.

    Called ONLY by GET /status -- nothing here runs during /start.

    The deviceRunStates record id is the composite "<script_id>:<device_id>", which is
    exactly the job_id /start hands back, so this is a direct equality match.

    Returned RAW on purpose: the orchestrator's _derive_intune reads detectionState,
    remediationState, lastStateUpdateDateTime and the script outputs itself, and uses
    lastStateUpdateDateTime to tell a fresh result from a stale one. Collapsing or
    rewording any of it here would break that.
    """
    target_id = f"{script_id}:{device_id}"
    url = (
        "https://graph.microsoft.com/beta/deviceManagement/deviceHealthScripts/"
        f"{script_id}/deviceRunStates"
    )
    headers = {"Authorization": f"Bearer {graph_token.get()}"}

    # Graph returns deviceRunStates a page at a time, so follow @odata.nextLink. Reading
    # only the first page means a device that sorts onto page 2 looks like "no record"
    # forever -- the orchestrator would keep polling and fail the job at its 20-minute
    # deadline even though the repair succeeded. Silent, and invisible until the script is
    # assigned to more than a page of devices.
    for page in range(MAX_RUN_STATE_PAGES):
        resp = _session.get(url, headers=headers, timeout=GRAPH_TIMEOUT)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Graph run-state query failed ({resp.status_code}): "
                       f"{resp.text[:300]}",
            )
        body = resp.json()

        for record in body.get("value", []):
            if record.get("id") == target_id:
                logger.info("run state found (page %s) for %s", page + 1, target_id)
                return record

        url = body.get("@odata.nextLink")
        if not url:
            break
    else:
        # Hit the cap. Logged, not swallowed -- otherwise it is indistinguishable from
        # "the device has not reported yet".
        logger.warning(
            "gave up after %s pages without finding %s", MAX_RUN_STATE_PAGES, target_id
        )

    # No record yet: the device has not reported since the trigger. A transient
    # detectionState is what the orchestrator reads as still-running, which is correct.
    logger.info("no run state yet for %s", target_id)
    return {
        "detectionState": "pending",
        "remediationState": "unknown",
        "lastStateUpdateDateTime": None,
    }


# ================================== API models ====================================

class StartRequest(BaseModel):
    """What the orchestrator posts. `input` arrives as a JSON *string*, not an object."""

    conversation_id: str = Field(min_length=1)
    input: str = ""


class StartResponse(BaseModel):
    job_id: str
    correlation_id: str


# ==================================== App =========================================

app = FastAPI(
    title="Diagnostics Agent API",
    description="KB -> Intune script mapping + on-demand remediation, for the "
                "orchestrator's async job contract.",
)


@app.get("/")
def health():
    return {
        "service": "diagnostics-agent",
        "mock": config.mock,
        "foundry_configured": bool(config.foundry_endpoint),
        "graph_configured": config.graph_ready,
    }


@app.post("/start", response_model=StartResponse)
def start(request: StartRequest):
    """Map the KB to a script and trigger remediation. Returns immediately.

    job_id is "<script_id>:<device_id>" -- everything /status needs to find the run
    state later, with no state kept in this service.
    """
    correlation_id = str(uuid.uuid4())

    try:
        payload = json.loads(request.input) if request.input else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="`input` is not valid JSON")

    kb_id = (payload.get("kb_id") or "").strip()
    device_id = (payload.get("device_id") or "").strip()

    if not kb_id:
        raise HTTPException(status_code=400, detail="`input.kb_id` is required")
    if not device_id:
        # See the CONTRACT NOTE at the top of this file.
        raise HTTPException(
            status_code=400,
            detail="`input.device_id` is required -- this service cannot know which "
                   "machine to remediate. Agree with the orchestrator team whether it "
                   "sends device_id, or user_id for us to resolve.",
        )

    logger.info(
        "start conv_id=%s kb_id=%s device_id=%s correlation_id=%s",
        request.conversation_id, kb_id, device_id, correlation_id,
    )

    if config.mock:
        job_id = f"MOCK-SCRIPT-{kb_id}:{device_id}"
        logger.warning("MOCK: no agent call, no Intune trigger -> %s", job_id)
        return StartResponse(job_id=job_id, correlation_id=correlation_id)

    mapping = agent_service.map_kb_to_script(device_id, kb_id)
    script_id = (mapping.get("script_id") or "UNKNOWN").strip()
    logger.info("Agent mapping: %s", mapping)

    if script_id in ("", "UNKNOWN"):
        raise HTTPException(
            status_code=422,
            detail=f"KB '{kb_id}' has no script mapping",
        )

    trigger_remediation(device_id, script_id)
    return StartResponse(job_id=f"{script_id}:{device_id}", correlation_id=correlation_id)


@app.get("/status")
def status(job_id: str):
    """Raw Intune run-state for a job_id produced by /start.

    Deliberately NOT normalised -- the orchestrator's _derive_intune reads
    detectionState / remediationState / lastStateUpdateDateTime and the script outputs,
    and uses lastStateUpdateDateTime to tell a fresh result from a stale one.
    """
    if ":" not in job_id:
        raise HTTPException(
            status_code=400,
            detail="job_id must be '<script_id>:<device_id>'",
        )
    script_id, device_id = job_id.split(":", 1)

    if config.mock:
        # Fabricated success, obviously labelled so it can never be mistaken for real
        # device output.
        return {
            "detectionState": "success",
            "remediationState": "success",
            "lastStateUpdateDateTime": "2026-08-19T12:00:00Z",
            "preRemediationDetectionScriptOutput": "MOCK: issue detected",
            "postRemediationDetectionScriptOutput": "MOCK: repaired on device "
                                                    f"{device_id}",
            "userMessage": "MOCK: the issue was fixed on your device.",
        }

    return fetch_run_state(script_id, device_id)
