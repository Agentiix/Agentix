"""Runtime worker client — one worker subprocess for remote callables.

Bridges the runtime server's Socket.IO handlers to the worker process.
Owns one worker subprocess per server process, routes calls by `call_id`,
shuts the worker down with the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from agentix.runtime.server.worker.invoker import CallableInvoker
from agentix.runtime.shared import frames as F
from agentix.runtime.shared.framing import read_frame, write_frame
from agentix.runtime.shared.models import RemoteError, RemoteRequest, RemoteResponse

logger = logging.getLogger("agentix.runtime.server.worker.client")

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


class WorkerBackend(Protocol):
    """Internal execution backend boundary."""

    async def call(self, request: RemoteRequest) -> RemoteResponse: ...
    async def shutdown(self) -> None: ...


TraceFrameHandler = Callable[[dict[str, Any]], None]
"""Called from the worker read loop for each F.TRACE frame. The SIO
layer installs one; the in-process backend has no transport hop."""


class _InProcessWorker:
    """In-process worker: resolves and calls fn in the server's own loop.
    Test fixture only — production routes through `_SubprocessWorker`."""

    def __init__(self) -> None:
        self._invoker = CallableInvoker()

    def _resolve_or_error(self, request: RemoteRequest) -> tuple[Any | None, RemoteError | None]:
        try:
            return request.callable.resolve(), None
        except Exception as exc:
            return None, RemoteError(type=type(exc).__name__, message=str(exc))

    async def call(self, request: RemoteRequest) -> RemoteResponse:
        fn, err = self._resolve_or_error(request)
        if err is not None:
            return RemoteResponse(ok=False, error=err)
        return await self._invoker.call(fn, request)

    async def shutdown(self) -> None:
        return


class _SubprocessWorker:
    """Single subprocess worker."""

    def __init__(
        self,
        python: str,
        runtime_bin_dir: Path | None = None,
        trace_handler: TraceFrameHandler | None = None,
    ) -> None:
        self._python = python
        self._runtime_bin_dir = runtime_bin_dir

        self._proc: asyncio.subprocess.Process | None = None
        self._send_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._boot_error: dict[str, Any] | None = None
        self._read_task: asyncio.Task | None = None
        self._closed = asyncio.Event()

        self._pending: dict[str, asyncio.Future] = {}
        self._cancel_tasks: set[asyncio.Task] = set()
        self._trace_handler = trace_handler

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
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(err.message))
            self._pending.clear()

    def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == F.READY:
            self._ready.set()
        elif kind == F.BOOT_ERROR:
            self._boot_error = frame.get("error") or {"type": "Unknown", "message": ""}
            self._ready.set()
        elif kind == F.RESULT:
            cid = frame.get("call_id", "")
            fut = self._pending.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(RemoteResponse(ok=True, value=frame.get("value")))
        elif kind == F.ERROR:
            cid = frame.get("call_id", "")
            err_payload = frame.get("error") or {"type": "Unknown", "message": ""}
            err = RemoteError.model_validate(err_payload)
            fut = self._pending.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(RemoteResponse(ok=False, error=err))
        elif kind == F.TRACE:
            if self._trace_handler is not None:
                try:
                    self._trace_handler(frame.get("frame") or {})
                except Exception:
                    logger.debug("trace handler raised; dropping", exc_info=True)
        else:
            logger.warning("runtime worker: unknown frame %r", kind)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        async with self._send_lock:
            await write_frame(self._proc.stdin, payload)

    def _call_frame(self, cid: str, request: RemoteRequest) -> dict[str, Any]:
        return {
            "type": F.CALL,
            "call_id": cid,
            "callable": str(request.callable),
            "arguments": request.arguments,
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

    async def call(self, request: RemoteRequest) -> RemoteResponse:
        cid = request.call_id or _new_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await self._send(self._call_frame(cid, request))
            return await fut
        finally:
            self._pending.pop(cid, None)
            if not fut.done():
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
    return uuid.uuid4().hex


class RuntimeWorkerClient:
    """Owns one worker process and routes all calls through it."""

    def __init__(self) -> None:
        self._python: str = sys.executable
        self._runtime_bin_dir: Path = Path(sys.executable).parent
        self._worker: WorkerBackend | None = None
        self._spawn_lock = asyncio.Lock()
        self._inprocess = _InProcessWorker()
        # Set by the SIO layer; invoked for each F.TRACE frame the
        # subprocess emits. Server has no trace state.
        self._trace_handler: TraceFrameHandler | None = None

    def set_trace_handler(self, handler: TraceFrameHandler | None) -> None:
        self._trace_handler = handler

    def _use_inprocess(self) -> None:
        self._worker = self._inprocess

    async def _get_worker(self) -> WorkerBackend:
        if self._worker is not None:
            return self._worker
        async with self._spawn_lock:
            if self._worker is not None:
                return self._worker
            worker = _SubprocessWorker(
                self._python,
                runtime_bin_dir=self._runtime_bin_dir,
                trace_handler=self._trace_handler,
            )
            await worker.start()
            self._worker = worker
            return worker

    async def shutdown(self) -> None:
        if self._worker is not None:
            await self._worker.shutdown()

    async def call(self, request: RemoteRequest) -> RemoteResponse:
        worker = await self._get_worker()
        return await worker.call(request)


__all__ = ["RuntimeWorkerClient", "WorkerBackend"]
