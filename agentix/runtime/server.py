"""Agentix runtime server.

The runtime server bundles:
  - built-in operations (exec/upload/download/ls) mounted at root
  - a closure loader + streaming reverse proxy for closures already mounted

Closures are static per sandbox: the deployment has mounted each closure at
`/mnt/<namespace>` before the runtime starts. On startup the runtime scans
/mnt and forks every closure it finds with an entry/bin/start. There is
no dynamic load/unload — sandbox contents are fixed at create time.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agentix import __version__
from agentix.models import HealthResponse, LogsResponse
from agentix.runtime.builtins import router as builtins_router
from agentix.runtime.loader import CLOSURE_MOUNT_ROOT, ClosureLoader

_HTTPX_UNREACHABLE = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TransportError,
)

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

loader = ClosureLoader()


async def _auto_load() -> None:
    """Scan /mnt for mounted closures and fork each one.

    The runtime itself is mounted at /mnt/runtime; skip it. Any other
    /mnt/<ns>/entry/bin/start is a closure the deployment arranged, and
    should be running. Failure here surfaces as a sandbox startup failure.
    """
    if not CLOSURE_MOUNT_ROOT.is_dir():
        return
    for ns_dir in sorted(CLOSURE_MOUNT_ROOT.iterdir()):
        if ns_dir.name == "runtime" or not ns_dir.is_dir():
            continue
        start = ns_dir / "entry" / "bin" / "start"
        if not (start.exists() and os.access(start, os.X_OK)):
            continue
        await loader.load(ns_dir.name)
        logger.info("Auto-loaded closure '%s'", ns_dir.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _auto_load()
    yield
    await loader.shutdown()


app = FastAPI(title="agentix", version=__version__, lifespan=lifespan)
app.state.loader = loader
app.include_router(builtins_router)


# ── Health ───────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(version=__version__)


# ── Closure introspection ───────────────────────────────────────


@app.get("/closures")
async def list_closures():
    return [c.model_dump() for c in loader.list_closures()]


@app.get("/closures/{namespace}/logs", response_model=LogsResponse)
async def closure_logs(namespace: str, tail: int | None = None) -> LogsResponse:
    try:
        stdout, stderr = loader.logs(namespace, tail=tail)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Closure '{namespace}' not loaded")
    return LogsResponse(namespace=namespace, stdout=stdout, stderr=stderr)


# ── Reverse proxy (catch-all) ────────────────────────────────────


@app.api_route(
    "/{namespace}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_to_closure(namespace: str, path: str, request: Request):
    """Streaming reverse proxy: /{namespace}/{path} → closure's Unix socket."""
    try:
        body = await request.body()
        status, headers, iterator, closer = await loader.proxy_stream(
            name=namespace,
            method=request.method,
            path=f"/{path}",
            headers=dict(request.headers),
            body=body if body else None,
            query=request.url.query or None,
        )
    except KeyError:
        return JSONResponse(
            status_code=502,
            content={"error": "closure not loaded", "namespace": namespace},
        )
    except _HTTPX_UNREACHABLE as exc:
        return JSONResponse(
            status_code=502,
            content={"error": f"closure unreachable: {exc}", "namespace": namespace},
        )

    async def _stream():
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            await closer()

    return StreamingResponse(
        _stream(),
        status_code=status,
        headers=headers,
        media_type=headers.get("content-type"),
    )


# ── Entry point (invoked as /mnt/runtime/entry/bin/start) ─────


def main() -> None:
    """Entry point the closure convention expects at
    /mnt/runtime/entry/bin/start. Port via AGENTIX_BIND_PORT (env, default
    8000); dev shell can override via --port.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="agentix runtime server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENTIX_BIND_PORT", "8000")),
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--debug-port", type=int, default=5678)
    parser.add_argument("--debug-wait", action="store_true")
    args = parser.parse_args()

    if args.debug:
        import debugpy

        debugpy.listen(("0.0.0.0", args.debug_port))
        print(f"debugpy listening on 0.0.0.0:{args.debug_port}")
        if args.debug_wait:
            print("Waiting for debugger to attach...")
            debugpy.wait_for_client()

    uvicorn.run("agentix.runtime.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
