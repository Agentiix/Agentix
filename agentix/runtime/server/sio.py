"""Socket.IO transport for the agentix runtime — msgpack payloads.

Every event's payload is a single `bytes` arg = msgpack-packed dict.

Wire:

  client → server:
    "call"        {call_id, callable, arguments}
    "cancel"      {call_id}

  server → client:
    "call:result" {call_id, value}     # value is pickle bytes
    "call:error"  {call_id, error}
    "trace:event" <TraceFrame dict>    # broadcast to all sessions
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import socketio
from pydantic import ValidationError

from agentix.runtime.server.worker import RuntimeWorkerClient
from agentix.runtime.shared.callables import RemoteCallable
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.events import (
    CALL,
    CALL_ERROR,
    CALL_RESULT,
    CANCEL,
    TRACE_EVENT,
)
from agentix.runtime.shared.idents import CallId
from agentix.runtime.shared.models import RemoteError, RemoteRequest

logger = logging.getLogger("agentix.runtime.sio")


def _u(data: Any) -> dict:
    if not data:
        return {}
    return unpack(bytes(data)) or {}


@dataclass
class _CallState:
    task: asyncio.Task


@dataclass
class _SessionState:
    calls: dict[str, _CallState] = field(default_factory=dict)


def make_sio(
    worker: RuntimeWorkerClient,
) -> tuple[socketio.AsyncServer, socketio.ASGIApp]:
    sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
    sessions: dict[str, _SessionState] = {}

    @sio.event
    async def connect(sid: str, environ: dict, auth: Any = None) -> None:
        sessions[sid] = _SessionState()
        logger.debug("sio connect %s", sid)

    @sio.event
    async def disconnect(sid: str) -> None:
        sess = sessions.pop(sid, None)
        if sess is None:
            return
        for call in sess.calls.values():
            call.task.cancel()
        await _drain_tasks([c.task for c in sess.calls.values()])
        logger.debug("sio disconnect %s", sid)

    @sio.on(CALL)
    async def on_call(sid: str, data: Any) -> None:
        sess = sessions.get(sid)
        if sess is None:
            return
        payload = _u(data)
        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            await sio.emit(CALL_ERROR, pack({
                "call_id": "", "error": {"type": "BadRequest", "message": "missing call_id"},
            }), to=sid)
            return

        async def _drive() -> None:
            try:
                request = RemoteRequest(
                    callable=RemoteCallable(payload["callable"]),
                    arguments=payload["arguments"],
                    call_id=CallId(call_id),
                )
            except (KeyError, ValidationError) as exc:
                await sio.emit(CALL_ERROR, pack({
                    "call_id": call_id,
                    "error": RemoteError(type=type(exc).__name__, message=str(exc)).model_dump(),
                }), to=sid)
                return
            resp = await worker.call(request)
            if resp.ok:
                await sio.emit(CALL_RESULT, pack({"call_id": call_id, "value": resp.value}), to=sid)
            else:
                error = (resp.error or RemoteError(type="Unknown", message="")).model_dump()
                await sio.emit(CALL_ERROR, pack({"call_id": call_id, "error": error}), to=sid)

        task = asyncio.create_task(_drive())
        sess.calls[call_id] = _CallState(task=task)
        task.add_done_callback(lambda _t: sess.calls.pop(call_id, None))

    @sio.on(CANCEL)
    async def on_cancel(sid: str, data: Any) -> None:
        sess = sessions.get(sid)
        if sess is None:
            return
        payload = _u(data)
        call_id = payload.get("call_id")
        call = sess.calls.pop(call_id, None) if isinstance(call_id, str) else None
        if call is not None:
            call.task.cancel()
            await sio.emit(CALL_ERROR, pack({
                "call_id": call_id,
                "error": RemoteError(
                    type="Cancelled",
                    message="remote call cancelled",
                    cancelled=True,
                ).model_dump(),
            }), to=sid)

    # ── trace passthrough ────────────────────────────────────────
    #
    # Worker → server → all sessions. Pure broadcast; the server holds
    # no trace state and doesn't know the frame's schema.

    _broadcast_tasks: set[asyncio.Task] = set()

    def _on_worker_trace_frame(trace_frame: dict[str, Any]) -> None:
        task = asyncio.create_task(sio.emit(TRACE_EVENT, pack(trace_frame)))
        _broadcast_tasks.add(task)
        task.add_done_callback(_broadcast_tasks.discard)

    worker.set_trace_handler(_on_worker_trace_frame)

    asgi_app = socketio.ASGIApp(sio, socketio_path="/socket.io")
    return sio, asgi_app


async def _drain_tasks(tasks: list[asyncio.Task]) -> None:
    if not tasks:
        return
    for t in tasks:
        if not t.done():
            t.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.gather(*tasks, return_exceptions=True)
