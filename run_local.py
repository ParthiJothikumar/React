"""Quick in-process smoke test - no server, no Core Tools needed.

Uses FastAPI's TestClient to call the app directly.
    pip install httpx   (TestClient needs it)
    python run_local.py
GET / needs no Cosmos/Foundry creds, so it's a safe first check.
"""
from fastapi.testclient import TestClient

from orchestrator_app import app

client = TestClient(app)

resp = client.get("/")
print("GET / ->", resp.status_code, resp.json())
