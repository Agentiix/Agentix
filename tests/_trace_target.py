"""Importable target for the cross-process span tree test.

Opens a parent span inside the worker and a nested child span that
emits a couple of attributes + a SpanEvent. The host should receive
all of these via the SIO trace bridge, with `call_id` stamped on the
worker-rooted spans for correlation.
"""

from __future__ import annotations

from agentix import trace


async def make_subtree(label: str) -> dict:
    with trace.span("worker.outer", label=label) as outer:
        with trace.span("worker.inner") as inner:
            inner.add_event("midpoint", note="halfway")
            inner.set_status("ok")
            inner.set_attribute("size", 42)
        outer.set_status("ok")
    return {"ok": True, "label": label}
