"""Namespace round-trip: plugin sandbox-side namespace talks to a
plugin host-side namespace handler."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from agentix import AsyncClientNamespace, RuntimeClient
from tests._namespace_target import (
    echo_via_namespace,
    emit_formatted_log,
    emit_log_line,
    emit_log_with_exception,
    emit_log_with_extra,
    fire_namespace_event,
)


class _EchoHost(AsyncClientNamespace):
    def __init__(self) -> None:
        super().__init__("/plugin-test")
        self.seen: list = []

    async def on_echo(self, data):
        self.seen.append(data)
        await self.emit(
            "echo:result",
            {
                "request_id": data["request_id"],
                "value": {"echoed": data["data"]},
            },
        )


@pytest.mark.asyncio
async def test_plugin_namespace_round_trip(live_server):
    base_url = await live_server()
    host_ns = _EchoHost()

    client = RuntimeClient(base_url)
    client.register_namespace(host_ns)
    async with client as c:
        result = await c.remote(echo_via_namespace, {"hello": 1})

    assert result == {"echoed": {"hello": 1}}
    assert len(host_ns.seen) == 1
    assert host_ns.seen[0]["data"] == {"hello": 1}


class _SlowHost(AsyncClientNamespace):
    """Host namespace whose `slow` handler blocks for a long time."""

    def __init__(self, hold: float) -> None:
        super().__init__("/plugin-test")
        self._hold = hold
        self.started = False
        self.finished = False

    async def on_slow(self, data):
        self.started = True
        await asyncio.sleep(self._hold)
        self.finished = True


@pytest.mark.asyncio
async def test_slow_namespace_handler_does_not_block_runtime(live_server):
    """A slow plugin handler must not stall the SIO receive loop —
    otherwise unrelated `c.remote` results queue up behind it.

    Regression: `socketio.AsyncClient` awaits `trigger_event` inline in
    its single websocket receive loop. `AsyncClientNamespace` detaches
    data-event handlers so a slow one can't freeze the connection.
    """
    base_url = await live_server()
    slow_host = _SlowHost(hold=30.0)

    client = RuntimeClient(base_url)
    client.register_namespace(slow_host)
    async with client as c:
        # Fire the event whose host handler sleeps 30s.
        await c.remote(fire_namespace_event, {"k": "v"})

        # Immediately do a normal RPC. If the slow handler blocked the
        # receive loop, this `call:result` would be stuck behind it for
        # ~30s. With the fix it returns near-instantly.
        t0 = time.perf_counter()
        result = await asyncio.wait_for(c.remote(abs, -5), timeout=10)
        elapsed = time.perf_counter() - t0

    assert result == 5
    assert elapsed < 8.0, f"runtime stalled behind slow handler: {elapsed:.1f}s"
    assert slow_host.started, "slow handler never ran"


@pytest.mark.asyncio
async def test_log_records_arrive_on_host(live_server):
    """Verify the full /log experience: plain messages, %-format args,
    extras dicts, and exception tracebacks all reach the host intact.
    Logger names + levelno round-trip so host filters see the sandbox
    record as if it had originated locally.
    """
    base_url = await live_server()

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name == "namespace_target":
                captured.append(record)

    target_logger = logging.getLogger("namespace_target")
    target_logger.setLevel(logging.INFO)
    handler = _Capture()
    target_logger.addHandler(handler)
    try:
        async with RuntimeClient(base_url) as c:
            await c.remote(emit_log_line, "from sandbox", "INFO")
            await c.remote(emit_formatted_log, "user %s acted on %s", "alice", "doc-7")
            await c.remote(emit_log_with_extra, "with extras", request_id="r-42", attempt=3)
            await c.remote(emit_log_with_exception, "caught one")
            # Let the /log pipe drain.
            await asyncio.sleep(0.5)
    finally:
        target_logger.removeHandler(handler)

    messages = {r.getMessage(): r for r in captured}

    # Plain log line.
    assert "from sandbox" in messages

    # %-style formatting: getMessage() already ran in the sandbox.
    assert "user alice acted on doc-7" in messages

    # extras kwargs survive — they show up as attributes on the record.
    extras_rec = messages.get("with extras")
    assert extras_rec is not None
    assert getattr(extras_rec, "request_id", None) == "r-42"
    assert getattr(extras_rec, "attempt", None) == 3

    # logger.exception() ships the formatted traceback in exc_text.
    exc_rec = messages.get("caught one")
    assert exc_rec is not None
    assert exc_rec.exc_text and "ValueError: kaboom" in exc_rec.exc_text
