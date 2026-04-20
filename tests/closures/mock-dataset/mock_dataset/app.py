"""Mock dataset closure used in integration tests and as a reference for
the closure ABI as a 'dataset': /setup and /verify.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from mock_dataset import __version__

app = FastAPI(title="mock-dataset", version=__version__)


MANIFEST = {
    "name": "mock-dataset",
    "version": __version__,
    "kind": "dataset",
    "description": "Mock dataset: setup returns an instruction, verify always passes.",
    "endpoints": [
        {"method": "POST", "path": "/setup", "description": "Return an agent_input for the given instance."},
        {"method": "POST", "path": "/verify", "description": "Return {pass: true, reason: ...}."},
    ],
}


@app.get("/")
async def manifest():
    return MANIFEST


@app.post("/setup")
async def setup(req: Request):
    body = await req.json()
    instance_id = body.get("instance_id", "unknown")
    return {
        "instruction": f"Solve instance {instance_id}",
        "workdir": "/workspace",
        "instance_id": instance_id,
    }


@app.post("/verify")
async def verify(req: Request):
    body = await req.json()
    patch = body.get("model_patch", body.get("patch", ""))
    return {
        "pass": True,
        "reason": f"mock verify (patch was {len(patch)} chars)",
    }
