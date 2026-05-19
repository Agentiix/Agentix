"""Runtime worker subprocess.

Receives CALL frames from the parent server over stdin, executes the
resolved callable, writes RESULT (or ERROR) frames to stdout. Trace
events emitted from inside the call go out as F.TRACE frames via the
side-channel forwarder installed at boot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from typing import Any

from agentix.runtime.server.worker.invoker import CallableInvoker
from agentix.runtime.shared import frames as F
from agentix.runtime.shared.callables import RemoteCallable
from agentix.runtime.shared.framing import read_frame, write_frame
from agentix.runtime.shared.idents import CallId
from agentix.runtime.shared.models import RemoteError, RemoteRequest
from agentix.trace._worker_bridge import DISPATCH_CALL_ID
from agentix.trace._worker_bridge import install as install_trace_bridge

logger = logging.getLogger("agentix.runtime.server.worker.process")


def _err(exc: BaseException) -> dict[str, Any]:
    return RemoteError(
        type=type(exc).__name__,
        message=str(exc),
        traceback=traceback.format_exc(),
    ).model_dump()


class Worker:
    """One process serving remote callable invocations."""

    def __init__(self) -> None:
        self._invoker = CallableInvoker()
        self._calls: dict[str, asyncio.Task] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._shutdown = asyncio.Event()
        self._outbound_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._drainer: asyncio.Task | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
        )
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer,
        )
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        self._reader, self._writer = reader, writer

        self._drainer = loop.create_task(self._drain_outbound())
        # Trace bridge: every Trace/Span lifecycle becomes an F.TRACE
        # frame on the outbound queue.
        install_trace_bridge(self._enqueue_trace_frame)
        await self._send({"type": F.READY})

        while not self._shutdown.is_set():
            try:
                frame = await read_frame(reader)
            except asyncio.IncompleteReadError:
                break
            if frame is None:
                break
            await self._handle(frame)

        for task in list(self._calls.values()):
            task.cancel()
        if self._calls:
            await asyncio.gather(*self._calls.values(), return_exceptions=True)
        await self._outbound_q.join()
        if self._drainer is not None:
            self._drainer.cancel()

    async def _drain_outbound(self) -> None:
        assert self._writer is not None
        try:
            while True:
                frame = await self._outbound_q.get()
                try:
                    await write_frame(self._writer, frame)
                except Exception:
                    logger.exception("outbound frame write failed")
                finally:
                    self._outbound_q.task_done()
        except asyncio.CancelledError:
            pass

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._outbound_q.put(payload)

    def _enqueue_trace_frame(self, trace_frame: dict[str, Any]) -> None:
        """Send callback for the trace bridge — sync put_nowait so
        trace call sites never block."""
        try:
            self._outbound_q.put_nowait({"type": F.TRACE, "frame": trace_frame})
        except Exception:
            logger.debug("failed to enqueue trace frame", exc_info=True)

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if not isinstance(kind, str):
            logger.warning("worker: missing frame type")
            return
        if kind == F.CALL:
            await self._on_call(frame)
        elif kind == F.CANCEL:
            self._cancel(frame.get("call_id", ""))
        elif kind == F.SHUTDOWN:
            self._shutdown.set()
        else:
            logger.warning("worker: unknown frame type %r", kind)

    async def _on_call(self, frame: dict[str, Any]) -> None:
        call_id = frame.get("call_id", "")
        try:
            request = RemoteRequest(
                callable=RemoteCallable(frame["callable"]),
                arguments=frame["arguments"],
                call_id=CallId(call_id) if call_id else None,
            )
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
            return
        task = asyncio.create_task(self._run(call_id, request))
        self._calls[call_id] = task
        task.add_done_callback(lambda _t: self._calls.pop(call_id, None))

    async def _run(self, call_id: str, request: RemoteRequest) -> None:
        try:
            fn = request.callable.resolve()
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
            return
        tok = DISPATCH_CALL_ID.set(call_id or None)
        try:
            resp = await self._invoker.call(fn, request)
        except Exception as exc:
            await self._send({"type": F.ERROR, "call_id": call_id, "error": _err(exc)})
            return
        finally:
            DISPATCH_CALL_ID.reset(tok)
        if resp.ok:
            await self._send({"type": F.RESULT, "call_id": call_id, "value": resp.value})
        else:
            err = (resp.error or RemoteError(type="Unknown", message="")).model_dump()
            await self._send({"type": F.ERROR, "call_id": call_id, "error": err})

    def _cancel(self, call_id: str) -> None:
        task = self._calls.get(call_id)
        if task is not None:
            task.cancel()
            asyncio.create_task(self._send({
                "type": F.ERROR,
                "call_id": call_id,
                "error": RemoteError(
                    type="Cancelled",
                    message="remote call cancelled",
                    cancelled=True,
                ).model_dump(),
            }))


async def _amain() -> None:
    worker = Worker()
    await worker.run()


def main() -> None:
    level_name = os.environ.get("AGENTIX_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
