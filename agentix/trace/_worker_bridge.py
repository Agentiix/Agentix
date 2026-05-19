"""Worker-side bridge: turn local Trace/Span lifecycle into `TraceFrame`
dicts and hand them to a transport-supplied send callback.

`install(send)` is the explicit hook the runtime worker calls at boot
to register the bridge as a `trace.Processor` against the global
provider. The bridge knows nothing about the runtime — only `send` and
the Trace data model.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any

from agentix import trace
from agentix.trace._wire import TraceFrame

# ContextVar populated by the runtime's dispatch wrapper just before
# user code runs. The forwarder reads it when stamping each frame so
# host-side consumers can correlate worker-emitted spans back to the
# originating RPC.
DISPATCH_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agentix_dispatch_call_id", default=None,
)


class WireForwardProcessor(trace.Processor):
    """Translates Trace/Span lifecycle into `TraceFrame` dicts and ships
    them via `send`. `send` is a sync callable (typically a
    `Queue.put_nowait`) — must not await or block."""

    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        self._send = send

    @staticmethod
    def _call_id() -> str | None:
        return DISPATCH_CALL_ID.get()

    def on_trace_start(self, t: trace.Trace) -> None:
        self._emit(TraceFrame(
            type="trace_start",
            trace_id=t.trace_id,
            call_id=self._call_id(),
            name=t.name,
            metadata=dict(t.metadata) if t.metadata else None,
            started_at=t.started_at,
        ))

    def on_trace_end(self, t: trace.Trace) -> None:
        self._emit(TraceFrame(
            type="trace_end",
            trace_id=t.trace_id,
            call_id=self._call_id(),
            ended_at=t.ended_at,
        ))

    def on_span_start(self, s: trace.Span) -> None:
        self._emit(TraceFrame(
            type="span_start",
            trace_id=s.trace_id,
            call_id=self._call_id(),
            span_id=s.span_id,
            parent_id=s.parent_id,
            name=s.name,
            attrs=dict(s.attrs) if s.attrs else None,
            started_at=s.started_at,
        ))

    def on_span_end(self, s: trace.Span) -> None:
        events_payload: list[dict[str, Any]] | None = None
        if s.events:
            events_payload = [
                {
                    "name": ev.name,
                    "timestamp": ev.timestamp,
                    "attributes": dict(ev.attributes) if ev.attributes else None,
                }
                for ev in s.events
            ]
        error_payload: dict[str, Any] | None = None
        if s.error is not None:
            error_payload = {
                "message": s.error.message,
                "data": dict(s.error.data) if s.error.data else None,
            }
        self._emit(TraceFrame(
            type="span_end",
            trace_id=s.trace_id,
            call_id=self._call_id(),
            span_id=s.span_id,
            parent_id=s.parent_id,
            name=s.name,
            attrs=dict(s.attrs) if s.attrs else None,
            started_at=s.started_at,
            ended_at=s.ended_at,
            status=s.status,
            status_description=s.status_description,
            error=error_payload,
            events=events_payload,
        ))

    def _emit(self, frame: TraceFrame) -> None:
        try:
            self._send(frame.model_dump(exclude_none=True))
        except Exception:
            pass


def install(send: Callable[[dict[str, Any]], None]) -> WireForwardProcessor:
    """Register a `WireForwardProcessor` against the global trace
    provider. The runtime worker boot calls this with its outbound
    queue's `put_nowait`. Returns the processor so the caller can
    later `trace.remove_processor(proc)` if needed."""
    proc = WireForwardProcessor(send)
    trace.add_processor(proc)
    return proc


__all__ = ["DISPATCH_CALL_ID", "WireForwardProcessor", "install"]
