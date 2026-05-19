"""Async client for the agentix runtime server.

The entire user surface:

    async with RuntimeClient(url) as c:
        result = await c.remote(fn, *args, **kwargs)

`fn` is any importable Python callable. The wire identifier is
`fn.__module__::fn.__qualname__`; args/kwargs travel as a single
pickle blob. Module-level functions, methods on importable classes,
and pickleable callable objects all work. Lambdas and local closures
do not — they can't round-trip through a name.
"""

from __future__ import annotations

import asyncio
import contextlib
import pickle
import uuid
from typing import Any

import httpx
import socketio

from agentix.runtime.shared.callables import RemoteCallable, display_name_for
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.events import (
    CALL,
    CALL_ERROR,
    CALL_RESULT,
    CANCEL,
    TRACE_EVENT,
)
from agentix.runtime.shared.models import HealthResponse, RemoteError
from agentix.trace._host_bridge import dispatch_frame as _dispatch_trace_frame


class RemoteCallError(RuntimeError):
    """Raised when a remote callable returns a non-ok RemoteResponse."""

    def __init__(self, display_name: str, error: RemoteError):
        super().__init__(f"{display_name}: {error.type}: {error.message}")
        self.display_name = display_name
        self.error = error


def _raise_remote_error(display_name: str, error: RemoteError):
    if error.cancelled:
        raise asyncio.CancelledError(error.message)
    raise RemoteCallError(display_name=display_name, error=error)


def _decode_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif isinstance(raw, bytearray):
        raw = bytes(raw)
    if isinstance(raw, bytes):
        return unpack(raw)
    return raw


class RuntimeClient:
    """Async client for the agentix runtime server."""

    def __init__(self, base_url: str, timeout: float = 300):
        self._base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        # Socket.IO bookkeeping — created lazily on first remote call.
        self._sio: socketio.AsyncClient | None = None
        self._sio_lock = asyncio.Lock()
        # call_id → queue of (kind, data) for in-flight calls.
        self._pending: dict[str, asyncio.Queue] = {}

    # ── lifecycle ────────────────────────────────────────────────

    async def close(self):
        if self._sio is not None and self._sio.connected:
            with contextlib.suppress(BaseException):
                await self._sio.disconnect()
        await self._client.aclose()

    async def __aenter__(self):
        await self._ensure_sio()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── public API ───────────────────────────────────────────────

    async def health(self) -> HealthResponse:
        r = await self._client.get("/health")
        r.raise_for_status()
        return HealthResponse.model_validate(r.json())

    async def remote(self, fn, *args, **kwargs):
        """Execute `fn(*args, **kwargs)` in the sandbox and return its result.

        `fn` must be importable on the worker side — `c.remote` doesn't
        send the function's code, only its `module::qualname` identifier.
        The worker resolves it via `import_module + getattr`.
        """
        display_name = display_name_for(fn)
        callable_ref = RemoteCallable._resolve(fn)
        arguments = pickle.dumps((args, kwargs))
        sio = await self._ensure_sio()
        call_id = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        self._pending[call_id] = q

        payload = {
            "call_id": call_id,
            "callable": str(callable_ref),
            "arguments": arguments,
        }
        terminated = False
        try:
            await sio.emit(CALL, pack(payload))
            while True:
                kind, data = await q.get()
                if kind == "result":
                    terminated = True
                    raw = data.get("value")
                    return pickle.loads(raw) if raw is not None else None
                if kind == "error":
                    err = RemoteError.model_validate(data["error"])
                    terminated = True
                    _raise_remote_error(display_name, err)
        finally:
            self._pending.pop(call_id, None)
            if not terminated:
                with contextlib.suppress(BaseException):
                    await sio.emit(CANCEL, pack({"call_id": call_id}))

    # ── Socket.IO connection management ─────────────────────────

    async def _ensure_sio(self) -> socketio.AsyncClient:
        if self._sio is not None and self._sio.connected:
            return self._sio
        async with self._sio_lock:
            if self._sio is not None and self._sio.connected:
                return self._sio
            sio = socketio.AsyncClient()

            async def _on_call_result(data):
                await self._route_event("result", data)

            async def _on_call_error(data):
                await self._route_event("error", data)

            sio.on(CALL_RESULT, _on_call_result)
            sio.on(CALL_ERROR, _on_call_error)

            async def _on_trace_event(data):
                # Pure passthrough — decode + dispatch to host's
                # `agentix.trace` processors. No state here.
                _dispatch_trace_frame(_decode_payload(data))
            sio.on(TRACE_EVENT, _on_trace_event)

            await sio.connect(self._base_url)
            self._sio = sio
            return sio

    async def _route_event(self, kind: str, raw: Any) -> None:
        data = _decode_payload(raw)
        call_id = data.get("call_id")
        q = self._pending.get(call_id) if isinstance(call_id, str) else None
        if q is not None:
            await q.put((kind, data))


__all__ = ["RemoteCallError", "RuntimeClient"]
