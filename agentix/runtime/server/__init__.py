"""Sandbox-side runtime server.

Composes FastAPI (for HTTP RPC + built-ins) and Socket.IO (for streams,
bidi, and log subscription) into the ASGI app uvicorn runs. Imports each
mounted closure's Python package lazily on first call.

Submodules:
  - `app`      — FastAPI app, lifespan, Registry, /_remote unary dispatch
  - `sio`      — Socket.IO server + event handlers + log forwarding
  - `builtins` — /exec, /upload, /download routes

Public names re-exported here so legacy imports keep working:
  `from agentix.runtime.server import app, main, registry`
  `await agentix.runtime.server._auto_load()`  (used by tests)
"""

from agentix.runtime.server.app import (
    CLOSURE_MOUNT_ROOT,
    _auto_load,
    _read_manifest,
    app,
    main,
    registry,
)

__all__ = [
    "CLOSURE_MOUNT_ROOT",
    "_auto_load",
    "_read_manifest",
    "app",
    "main",
    "registry",
]
