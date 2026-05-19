"""Host-side bridge: decode incoming `TraceFrame` dicts and dispatch
the reconstructed `Trace`/`Span` to the global trace provider.

`dispatch_frame(raw_dict)` is the only public function. The runtime's
SIO event handler calls it for each `trace:event` broadcast; the
bridge knows nothing about Socket.IO.
"""

from __future__ import annotations

from typing import Any

from agentix import trace
from agentix.trace._wire import TraceFrame


def dispatch_frame(raw: dict[str, Any]) -> None:
    """Decode one wire frame and fan it out to host-side processors.

    Malformed payloads are silently dropped — never kill the upstream
    SIO callback that called us."""
    try:
        frame = TraceFrame.model_validate(raw)
    except Exception:
        return

    provider = trace._provider

    if frame.type == "trace_start":
        provider.fan_trace_start(trace.Trace(
            trace_id=frame.trace_id,
            name=frame.name or "",
            metadata=dict(frame.metadata) if frame.metadata else {},
            started_at=frame.started_at,
        ))

    elif frame.type == "trace_end":
        provider.fan_trace_end(trace.Trace(
            trace_id=frame.trace_id,
            name=frame.name or "",
            metadata=dict(frame.metadata) if frame.metadata else {},
            ended_at=frame.ended_at,
        ))

    elif frame.type == "span_start":
        attrs = dict(frame.attrs) if frame.attrs else {}
        if frame.call_id is not None:
            attrs.setdefault("call_id", frame.call_id)
        provider.fan_span_start(trace.Span(
            span_id=frame.span_id or "",
            trace_id=frame.trace_id,
            parent_id=frame.parent_id,
            name=frame.name or "",
            attrs=attrs,
            started_at=frame.started_at,
        ))

    elif frame.type == "span_end":
        attrs = dict(frame.attrs) if frame.attrs else {}
        if frame.call_id is not None:
            attrs.setdefault("call_id", frame.call_id)
        s = trace.Span(
            span_id=frame.span_id or "",
            trace_id=frame.trace_id,
            parent_id=frame.parent_id,
            name=frame.name or "",
            attrs=attrs,
            started_at=frame.started_at,
            ended_at=frame.ended_at,
            status=frame.status or "unset",  # type: ignore[arg-type]
            status_description=frame.status_description,
        )
        if frame.error:
            s.error = trace.SpanError(
                message=str(frame.error.get("message", "")),
                data=frame.error.get("data"),
            )
        if frame.events:
            s.events = [
                trace.SpanEvent(
                    name=str(ev.get("name", "")),
                    timestamp=str(ev.get("timestamp", "")),
                    attributes=dict(ev.get("attributes") or {}),
                )
                for ev in frame.events
            ]
        provider.fan_span_end(s)


__all__ = ["dispatch_frame"]
