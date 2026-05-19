"""Importable target for the concurrent-remote test.

Opens a span around an `asyncio.sleep`; the worker's WireForwardProcessor
ships span_start/end frames to the host. If multiple concurrent dispatches
share an event loop in the worker, their sleeps interleave and total wall
time is dominated by one sleep instead of the sum.
"""

from __future__ import annotations

import asyncio

from agentix import trace


async def emit_and_sleep(label: str, duration: float) -> dict:
    with trace.span("concurrent_test", label=label, duration=duration) as s:
        await asyncio.sleep(duration)
        s.set_status("ok")
    return {"label": label}
