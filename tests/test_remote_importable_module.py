"""Remote calls can target regular importable modules.

`from my_app.tasks import fn; c.remote(fn, ...)` works when the module
is importable in the runtime venv.
"""

from __future__ import annotations

from agentix import RuntimeClient
from agentix.runtime.server.worker_client import RuntimeWorkerClient
from agentix.runtime.shared.models import RemoteRequest

_USER_MODULE = "tests._user_app_target"


async def test_remote_call_to_importable_module():
    """Remote call to `tests._user_app_target`."""
    mp = RuntimeWorkerClient()

    try:
        resp = await mp.call_unary(RemoteRequest(
            target=f"{_USER_MODULE}::greet", kwargs={"name": "world"},
        ))
        assert resp.ok, resp.error
        assert resp.value == "hello world"

        # Second function on the same module should reuse the same worker.
        resp2 = await mp.call_unary(RemoteRequest(
            target=f"{_USER_MODULE}::add", kwargs={"a": 3, "b": 4},
        ))
        assert resp2.ok, resp2.error
        assert resp2.value == 7

    finally:
        await mp.shutdown()


async def test_client_remote_accepts_imported_function(live_server):
    from tests._user_app_target import greet

    base_url = await live_server()
    async with RuntimeClient(base_url) as c:
        assert await c.remote(greet, name="world") == "hello world"


async def test_unimportable_module_returns_module_not_loaded():
    """If a remote call arrives for an unimportable module, return ModuleNotLoaded."""
    mp = RuntimeWorkerClient()

    try:
        resp = await mp.call_unary(RemoteRequest(
            target="this.module.really.does.not.exist::anything",
        ))
        assert not resp.ok
        assert resp.error is not None
        assert resp.error.type == "ModuleNotLoaded"
    finally:
        await mp.shutdown()
