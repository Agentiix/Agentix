"""Runtime worker client — one worker subprocess for all remote targets.

This module bridges FastAPI/Socket.IO handlers to the worker process. It owns
one worker subprocess per runtime server process, routes calls by `call_id`,
and shuts the worker down with the server.

The worker imports the requested target module dynamically for each call,
so the dependency model stays simple: anything installed into
`/nix/runtime` can be called through `RuntimeClient.remote(fn, ...)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from agentix.invoke import FunctionInvoker
from agentix.runtime.shared import frames as F
from agentix.runtime.shared.framing import read_frame, write_frame
from agentix.runtime.shared.models import RemoteError, RemoteRequest, RemoteResponse

logger = logging.getLogger("agentix.runtime.server.worker_client")

_WORKER_START_TIMEOUT = 15.0
_DEFAULT_WORKER_PATH = "/usr/local/bin:/usr/bin:/bin"
_STRIPPED_ENV = {
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONPATH",
    "PYTHONHOME",
    "LOCALE_ARCHIVE",
    "SSL_CERT_FILE",
}
_STRIPPED_ENV_PREFIXES = ("NIX_", "FONTCONFIG_")


def _clean_worker_env(runtime_bin_dir: Path | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _STRIPPED_ENV
        and not any(key.startswith(prefix) for prefix in _STRIPPED_ENV_PREFIXES)
    }
    env["PATH"] = (
        f"{runtime_bin_dir}:{_DEFAULT_WORKER_PATH}"
        if runtime_bin_dir is not None
        else _DEFAULT_WORKER_PATH
    )
    return env


class _WorkerLike(Protocol):
    async def call_unary(self, request: RemoteRequest) -> RemoteResponse: ...
    def iter_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]: ...
    def iter_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def shutdown(self) -> None: ...


class _InProcessWorker:
    """Test worker that calls explicitly registered targets."""

    def __init__(self) -> None:
        self._invokers: dict[str, FunctionInvoker] = {}

    def register(self, module: str, invoker: FunctionInvoker) -> None:
        self._invokers[module] = invoker

    def has(self, module: str) -> bool:
        return module in self._invokers

    def _invoker_for(self, request: RemoteRequest) -> FunctionInvoker | None:
        return self._invokers.get(request.module)

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse:
        invoker = self._invoker_for(request)
        if invoker is None:
            return RemoteResponse(ok=False, error=_module_not_loaded(request.module))
        return await invoker.call_unary(request)

    async def iter_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        invoker = self._invoker_for(request)
        if invoker is None:
            yield {"type": "error", "error": _module_not_loaded(request.module).model_dump()}
            return
        async for ev in invoker.call_stream(request):
            yield ev

    async def iter_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        invoker = self._invoker_for(request)
        if invoker is None:
            yield {"type": "error", "error": _module_not_loaded(request.module).model_dump()}
            return
        adapter = invoker.input_adapter_for(request.function)  # type: ignore[arg-type]

        async def _coerced():
            async for raw in input_iter:
                if adapter is not None:
                    raw = adapter.validate_python(raw)
                yield raw

        async for ev in invoker.call_bidi(request, _coerced()):
            yield ev

    async def shutdown(self) -> None:
        return


class _SubprocessWorker:
    """Single subprocess worker for the runtime."""

    def __init__(
        self,
        python: str,
        runtime_bin_dir: Path | None = None,
    ) -> None:
        self._python = python
        self._runtime_bin_dir = runtime_bin_dir

        self._proc: asyncio.subprocess.Process | None = None
        self._send_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._boot_error: dict[str, Any] | None = None
        self._read_task: asyncio.Task | None = None
        self._closed = asyncio.Event()

        self._unary: dict[str, asyncio.Future] = {}
        self._streams: dict[str, asyncio.Queue] = {}
        self._cancel_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        env = _clean_worker_env(self._runtime_bin_dir)
        self._proc = await asyncio.create_subprocess_exec(
            self._python, "-m", "agentix.runtime.server.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            env=env,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        ready_task = asyncio.create_task(self._ready.wait())
        closed_task = asyncio.create_task(self._closed.wait())
        assert self._proc is not None
        proc_task = asyncio.create_task(self._proc.wait())
        try:
            done, pending = await asyncio.wait(
                {ready_task, closed_task, proc_task},
                timeout=_WORKER_START_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if not done:
                await self.shutdown()
                raise TimeoutError(
                    f"runtime worker did not become ready within "
                    f"{_WORKER_START_TIMEOUT:.0f}s"
                )
            if ready_task not in done:
                rc = self._proc.returncode
                await self.shutdown()
                detail = f"exit code {rc}" if rc is not None else "stdout closed"
                raise RuntimeError(f"runtime worker exited before ready ({detail})")
        finally:
            for task in (ready_task, closed_task, proc_task):
                if not task.done():
                    task.cancel()
        if self._boot_error is not None:
            await self.shutdown()
            raise RuntimeError(
                "runtime worker failed to boot: "
                f"{self._boot_error.get('type')}: {self._boot_error.get('message')}"
            )

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                frame = await read_frame(self._proc.stdout)
                if frame is None:
                    break
                self._on_frame(frame)
        except Exception:
            logger.exception("runtime worker read loop crashed")
        finally:
            self._closed.set()
            err = RemoteError(type="WorkerExited", message="runtime worker exited")
            for fut in list(self._unary.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(err.message))
            self._unary.clear()
            for q in list(self._streams.values()):
                q.put_nowait({"type": "error", "error": err.model_dump()})

    def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == F.READY:
            self._ready.set()
        elif kind == F.BOOT_ERROR:
            self._boot_error = frame.get("error") or {"type": "Unknown", "message": ""}
            self._ready.set()
        elif kind == F.RESULT:
            cid = frame.get("call_id", "")
            fut = self._unary.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(RemoteResponse(ok=True, value=frame.get("value")))
        elif kind == F.ERROR:
            cid = frame.get("call_id", "")
            err_payload = frame.get("error") or {"type": "Unknown", "message": ""}
            err = RemoteError.model_validate(err_payload)
            fut = self._unary.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(RemoteResponse(ok=False, error=err))
                return
            q = self._streams.get(cid)
            if q is not None:
                q.put_nowait({"type": "error", "error": err_payload})
        elif kind == F.STREAM_ITEM:
            q = self._streams.get(frame.get("call_id", ""))
            if q is not None:
                q.put_nowait({"type": "item", "value": frame.get("value")})
        elif kind == F.STREAM_END:
            q = self._streams.get(frame.get("call_id", ""))
            if q is not None:
                q.put_nowait({"type": "end"})
        else:
            logger.warning("runtime worker: unknown frame %r", kind)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        async with self._send_lock:
            await write_frame(self._proc.stdin, payload)

    def _call_frame(self, kind: str, cid: str, request: RemoteRequest) -> dict[str, Any]:
        return {
            "type": F.CALL,
            "kind": kind,
            "call_id": cid,
            "target": request.target,
            "args": request.args,
            "kwargs": request.kwargs,
        }

    def _schedule_cancel(self, cid: str) -> None:
        t = asyncio.create_task(self._send_cancel(cid))
        self._cancel_tasks.add(t)
        t.add_done_callback(self._cancel_tasks.discard)

    async def _send_cancel(self, cid: str) -> None:
        try:
            await self._send({"type": F.CANCEL, "call_id": cid})
        except Exception:
            logger.debug("cancel send failed for call %r", cid)

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse:
        cid = request.call_id or _new_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._unary[cid] = fut
        try:
            await self._send(self._call_frame(F.KIND_UNARY, cid, request))
            return await fut
        finally:
            self._unary.pop(cid, None)
            if not fut.done():
                self._schedule_cancel(cid)

    async def iter_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        cid = request.call_id or _new_id()
        q: asyncio.Queue = asyncio.Queue()
        self._streams[cid] = q
        terminated = False
        try:
            await self._send(self._call_frame(F.KIND_STREAM, cid, request))
            while True:
                ev = await q.get()
                yield ev
                if ev.get("type") in ("end", "error"):
                    terminated = True
                    return
        finally:
            self._streams.pop(cid, None)
            if not terminated:
                self._schedule_cancel(cid)

    async def iter_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        cid = request.call_id or _new_id()
        q: asyncio.Queue = asyncio.Queue()
        self._streams[cid] = q
        input_task: asyncio.Task | None = None
        terminated = False
        try:
            await self._send(self._call_frame(F.KIND_BIDI, cid, request))

            async def _pump_input() -> None:
                try:
                    async for item in input_iter:
                        await self._send({"type": F.BIDI_IN, "call_id": cid, "item": item})
                finally:
                    await self._send({"type": F.BIDI_END_IN, "call_id": cid})

            input_task = asyncio.create_task(_pump_input())
            while True:
                ev = await q.get()
                yield ev
                if ev.get("type") in ("end", "error"):
                    terminated = True
                    return
        finally:
            self._streams.pop(cid, None)
            if input_task is not None:
                input_task.cancel()
                with contextlib.suppress(BaseException):
                    await input_task
            if not terminated:
                self._schedule_cancel(cid)

    async def shutdown(self) -> None:
        if self._proc is None:
            return
        try:
            await self._send({"type": F.SHUTDOWN})
        except Exception:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except TimeoutError:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(BaseException):
                await self._read_task


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _module_not_loaded(module: str) -> RemoteError:
    return RemoteError(
        type="ModuleNotLoaded",
        message=f"module not importable in the runtime venv: {module!r}",
    )


class RuntimeWorkerClient:
    """Owns one worker process and routes all runtime calls through it."""

    def __init__(self) -> None:
        self._python: str = sys.executable
        self._runtime_bin_dir: Path = Path(sys.executable).parent
        self._worker: _WorkerLike | None = None
        self._spawn_lock = asyncio.Lock()
        self._inprocess = _InProcessWorker()

    def _register_inprocess(self, target: Any) -> None:
        module = target.__name__ if isinstance(target, ModuleType) else target.__module__
        self._inprocess.register(module, FunctionInvoker(target))
        self._worker = self._inprocess

    def has(self, module: str) -> bool:
        return self._inprocess.has(module)

    async def _get_worker(self) -> _WorkerLike:
        if self._worker is not None:
            return self._worker
        async with self._spawn_lock:
            if self._worker is not None:
                return self._worker
            worker = _SubprocessWorker(
                self._python,
                runtime_bin_dir=self._runtime_bin_dir,
            )
            await worker.start()
            self._worker = worker
            return worker

    async def shutdown(self) -> None:
        if self._worker is not None:
            await self._worker.shutdown()

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse:
        worker = await self._get_worker()
        return await worker.call_unary(request)

    async def call_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        worker = await self._get_worker()
        async for ev in worker.iter_stream(request):
            yield ev

    async def call_bidi(
        self, request: RemoteRequest, input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        worker = await self._get_worker()
        async for ev in worker.iter_bidi(request, input_iter):
            yield ev


__all__ = ["RuntimeWorkerClient"]
