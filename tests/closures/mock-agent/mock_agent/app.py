"""Mock agent closure used in integration tests and as a reference for the
closure ABI: a single POST /run endpoint that fakes a patch.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from mock_agent import __version__

app = FastAPI(title="mock-agent", version=__version__)


MANIFEST = {
    "name": "mock-agent",
    "version": __version__,
    "kind": "agent",
    "description": "Mock agent: returns the instruction as a fake patch.",
    "endpoints": [
        {"method": "POST", "path": "/run", "description": "Run against an instruction. Body: {instruction, workdir?}"},
    ],
}


@app.get("/")
async def manifest():
    return MANIFEST


@app.post("/run")
async def run(req: Request):
    body = await req.json()
    instruction = body.get("instruction", "")
    workdir = body.get("workdir", "/")
    return {
        "exit_code": 0,
        "patch": f"# mock patch\n# workdir={workdir}\n# instruction={instruction}\n",
    }
