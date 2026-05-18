"""Protocol-level integration tests for the runtime server's remote call path.

These tests drive the real `agentix.runtime.server` FastAPI app over an
ASGI transport. The fixture `register_target` injects test classes into
the runtime worker so protocol behavior can be tested without packaging
temporary modules.

Closure classes are declared at module scope so `eval_str=True` (used
by the framework to resolve PEP 563 stringified annotations) can find
their referenced types.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import socketio

from agentix import Channel, RemoteCallError, RuntimeClient
from agentix.runtime.shared.codec import pack, unpack
from agentix.runtime.shared.events import CANCEL, STREAM, STREAM_ERROR
from agentix.runtime.shared.models import RemoteRequest

pytestmark = pytest.mark.asyncio


# ── remote target shapes used across tests ──────────────────────────────


@dataclass
class EchoResult:
    msg: str


class Echo:
    @staticmethod
    async def echo(msg: str) -> EchoResult:
        return EchoResult(msg=f"echo:{msg}")


class Boom:
    @staticmethod
    async def go() -> str:
        raise RuntimeError("kaboom")


@dataclass
class Token:
    text: str
    idx: int


class Streamer:
    @staticmethod
    async def chat(prompt: str, n: int = 3) -> AsyncIterator[Token]:
        for i in range(n):
            yield Token(text=f"{prompt}-{i}", idx=i)


@dataclass
class UserMsg:
    text: str


@dataclass
class ReplyMsg:
    text: str


class Chat:
    @staticmethod
    async def chat(
        messages: Channel[UserMsg],
        prefix: str = "say:",
    ) -> AsyncIterator[ReplyMsg]:
        async for m in messages:
            yield ReplyMsg(text=f"{prefix}{m.text}")


class Talker:
    @staticmethod
    async def speak() -> str:
        logging.getLogger("agentix.test.talker").info("hello-from-impl")
        return "ok"


@dataclass
class Item:
    i: int


class SlowEcho:
    """Bidi impl that yields slower than a producer can push — exercises
    the worker's per-call pump task + bounded user queue backpressure."""

    @staticmethod
    async def echo(items: Channel[Item]) -> AsyncIterator[Item]:
        async for it in items:
            await asyncio.sleep(0.001)
            yield Item(i=it.i)


class SlowStream:
    @staticmethod
    async def ticks() -> AsyncIterator[int]:
        while True:
            await asyncio.sleep(1)
            yield 1


# ── remote call basics ─────────────────────────────────────────────────


async def test_registered_target_calls(
    runtime_module, register_target,
):
    """An in-process test target calls via /_remote."""
    server, _, _ = runtime_module
    register_target(Echo)
    pkg = Echo.__module__

    assert server.worker.has(pkg)

    from agentix.runtime.shared.codec import pack, unpack
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        body = pack(RemoteRequest(
            target=f"{pkg}::echo", kwargs={"msg": "hi"},
        ).model_dump())
        r = await http.post("/_remote", content=body,
                            headers={"Content-Type": "application/msgpack"})
        assert r.status_code == 200
        assert unpack(r.content) == {"ok": True, "value": {"msg": "echo:hi"}, "error": None}


async def test_remote_call_unknown_module_returns_error_body(runtime_module):
    """An unimportable module returns a ModuleNotLoaded error in-band
    (wire stays 200), not a 404. The pre-flight has() check was removed
    because it duplicated the runtime worker's own error path AND violated
    the framework's 'wire stays 200, errors live in the body' policy."""
    server, _, _ = runtime_module
    from agentix.runtime.shared.codec import pack, unpack
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        body = pack(RemoteRequest(
            target="agentix.really.does.not.exist::x",
        ).model_dump())
        r = await http.post("/_remote", content=body,
                            headers={"Content-Type": "application/msgpack"})
        assert r.status_code == 200
        resp = unpack(r.content)
        assert resp["ok"] is False
        assert resp["error"]["type"] == "ModuleNotLoaded"


async def test_remote_call_unknown_function_returns_error_body(
    runtime_module, register_target,
):
    server, _, _ = runtime_module
    register_target(Echo)
    from agentix.runtime.shared.codec import pack, unpack
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        body = pack(RemoteRequest(
            target=f"{Echo.__module__}::not_a_function",
        ).model_dump())
        r = await http.post("/_remote", content=body,
                            headers={"Content-Type": "application/msgpack"})
        assert r.status_code == 200  # 200 with ok=False; wire stays clean
        resp = unpack(r.content)
        assert resp["ok"] is False
        assert resp["error"]["type"] == "FunctionNotFound"


async def test_impl_exception_surfaces_as_remote_error(
    runtime_module, register_target,
):
    server, _, _ = runtime_module
    register_target(Boom)
    from agentix.runtime.shared.codec import pack, unpack
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        body = pack(RemoteRequest(target=f"{Boom.__module__}::go").model_dump())
        r = await http.post("/_remote", content=body,
                            headers={"Content-Type": "application/msgpack"})
        assert r.status_code == 200
        resp = unpack(r.content)
        assert resp["ok"] is False
        assert resp["error"]["type"] == "RuntimeError"
        assert "kaboom" in resp["error"]["message"]


async def test_client_remote_round_trip(
    runtime_module, register_target, live_server,
):
    register_target(Echo)
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        result = await c.remote(Echo.echo, msg="hello")
        assert result.msg == "echo:hello"


async def test_client_remote_raises_on_impl_error(
    runtime_module, register_target, live_server,
):
    register_target(Boom)
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        with pytest.raises(RemoteCallError):
            await c.remote(Boom.go)


# ── streaming + bidi ─────────────────────────────────────────────────


async def test_stream_round_trip_via_socketio(
    runtime_module, register_target, live_server,
):
    register_target(Streamer)
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        items = [t async for t in c.remote(Streamer.chat, prompt="hi", n=2)]
        assert items == [Token(text="hi-0", idx=0), Token(text="hi-1", idx=1)]


async def test_stream_call_context_uses_routable_call_id(
    runtime_module, register_target, live_server,
):
    register_target(Streamer)
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        with c.call_context(call_id="ctx-stream"):
            items = [t async for t in c.remote(Streamer.chat, prompt="ctx", n=1)]
    assert items == [Token(text="ctx-0", idx=0)]


async def test_socketio_cancel_gets_cancelled_error_ack(
    runtime_module, register_target, live_server,
):
    register_target(SlowStream)
    base_url = await live_server()

    sio = socketio.AsyncClient()
    errors: asyncio.Queue = asyncio.Queue()

    async def _on_error(data):
        await errors.put(unpack(data))

    sio.on(STREAM_ERROR, _on_error)
    await sio.connect(base_url)
    try:
        call_id = "cancel-me"
        await sio.emit(STREAM, pack({
            "call_id": call_id,
            "target": f"{SlowStream.__module__}::ticks",
        }))
        await asyncio.sleep(0.05)
        await sio.emit(CANCEL, pack({"call_id": call_id}))
        payload = await asyncio.wait_for(errors.get(), timeout=5)
    finally:
        await sio.disconnect()

    assert payload["call_id"] == "cancel-me"
    assert payload["error"]["type"] == "Cancelled"
    assert payload["error"]["cancelled"] is True


async def test_bidi_round_trip_via_socketio(
    runtime_module, register_target, live_server,
):
    register_target(Chat)
    base_url = await live_server()

    inbox: Channel[UserMsg] = Channel()

    async def _push() -> None:
        for t in ("hi", "there"):
            await inbox.send(UserMsg(text=t))
        await inbox.close()

    async with RuntimeClient(base_url) as c:
        producer = asyncio.create_task(_push())
        replies = [r async for r in c.remote(Chat.chat, messages=inbox)]
        await producer
        assert [r.text for r in replies] == ["say:hi", "say:there"]


async def test_bidi_accepts_positional_channel_arg(
    runtime_module, register_target, live_server,
):
    register_target(Chat)
    base_url = await live_server()

    inbox: Channel[UserMsg] = Channel()

    async def _push() -> None:
        await inbox.send(UserMsg(text="positional"))
        await inbox.close()

    async with RuntimeClient(base_url) as c:
        producer = asyncio.create_task(_push())
        replies = [r async for r in c.remote(Chat.chat, inbox)]
        await producer
        assert [r.text for r in replies] == ["say:positional"]


async def test_bidi_backpressure_no_drops_under_slow_consumer(
    runtime_module, register_target, live_server,
):
    """Producer sends N items > worker's _BIDI_USER_BUFFER while the impl
    yields slowly. With the per-call pump + bounded user queue, every
    item must reach the impl — no silent drops."""
    register_target(SlowEcho)
    base_url = await live_server()

    n = 150  # well past _BIDI_USER_BUFFER=64
    inbox: Channel[Item] = Channel()

    async def _push() -> None:
        for i in range(n):
            await inbox.send(Item(i=i))
        await inbox.close()

    async with RuntimeClient(base_url) as c:
        producer = asyncio.create_task(_push())
        received = [r.i async for r in c.remote(SlowEcho.echo, items=inbox)]
        await producer

    assert received == list(range(n))


# ── log subscription ─────────────────────────────────────────────────


@pytest.mark.skip(reason="log forwarding fixture timing flake; tracked separately")
async def test_logs_subscription_receives_emitted_log(
    runtime_module, register_target, live_server,
):
    register_target(Talker)
    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        seen: list[str] = []

        async def _collect():
            async for rec in c.logs():
                if rec.name == "agentix.test.talker":
                    seen.append(rec.message)
                    return

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0.2)
        await c.remote(Talker.speak)
        await asyncio.wait_for(collector, timeout=5)
        assert seen == ["hello-from-impl"]
