"""Trace wire envelope.

Carries Trace/Span/SpanEvent lifecycle across a process boundary. The
runtime layer ships this as an opaque dict — it has no idea what the
fields mean. Both bridge processors (worker forwarder, host receiver)
encode/decode against this model.

Lives in `agentix.trace.*` rather than `agentix.runtime.shared.*` so
the runtime layer stays trace-unaware. The runtime only knows there
exists a frame type tagged `F.TRACE` and a Socket.IO event named
`TRACE_EVENT` — both are routing labels, no semantic content.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class TraceFrame(BaseModel):
    """Wire envelope for one Trace/Span lifecycle event.

    Discriminated by `type`. Worker forwarder fills the relevant subset
    of fields per event; host receiver reconstructs `Trace`/`Span`
    instances and dispatches them to local Processors.

    `call_id` is stamped by the worker's dispatch wrapper (via a
    contextvar) so host-side consumers can correlate worker-emitted
    spans back to their originating `c.remote(...)` call without any
    field on `RemoteRequest`.
    """

    type: Literal["trace_start", "trace_end", "span_start", "span_end"]
    trace_id: str
    call_id: str | None = None
    # Lifecycle timestamps.
    started_at: str | None = None
    ended_at: str | None = None
    # Trace fields (set on trace_start/end).
    name: str | None = None
    metadata: dict[str, Any] | None = None
    # Span fields.
    span_id: str | None = None
    parent_id: str | None = None
    attrs: dict[str, Any] | None = None
    status: str | None = None
    status_description: str | None = None
    error: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None


__all__ = ["TraceFrame"]
