"""Closure loader: spawn closure processes, reverse-proxy via Unix socket.

Each closure image is mounted at `/mnt/<namespace>` in the sandbox and
carries `/mnt/<namespace>/store/...` (Nix deps) and
`/mnt/<namespace>/entry/...` (derivation root — bin/lib/etc tree).

Loading a closure is: fork `/mnt/<namespace>/entry/bin/start` with
PATH prefixed by `/mnt/<namespace>/entry/bin` and a socket path
passed via env. The loader owns the process lifecycle, a bounded
stdout/stderr ring buffer, and the HTTP client used by the server's
reverse proxy.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from agentix.models import ClosureInfo, ClosureManifest

logger = logging.getLogger("agentix.runtime.loader")

SOCKET_DIR = Path(os.environ.get("AGENTIX_SOCKET_DIR", "/tmp/agentix"))
CLOSURE_MOUNT_ROOT = Path(os.environ.get("AGENTIX_CLOSURE_MOUNT_ROOT", "/mnt"))
LOG_BUFFER_BYTES = int(os.environ.get("AGENTIX_LOG_BUFFER_BYTES", str(1 * 1024 * 1024)))  # 1 MiB per stream

# Env vars scrubbed before forking a closure subprocess. The runtime is a
# Nix-built binary, so os.environ is pre-loaded with Nix-runtime paths that
# would ABI-clash with binaries under /mnt/<ns>/entry/bin.
_RUNTIME_ONLY_ENV = {
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PYTHONPATH",
    "PYTHONHOME",
    "LOCALE_ARCHIVE",
    "FONTCONFIG_FILE",
    "FONTCONFIG_PATH",
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
}


def _scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _RUNTIME_ONLY_ENV and not k.startswith("NIX_")
    }
    if extra:
        env.update(extra)
    return env


class _RingBuffer:
    """Bounded byte ring buffer backed by a deque of chunks."""

    def __init__(self, max_bytes: int):
        self._max = max_bytes
        self._chunks: collections.deque[bytes] = collections.deque()
        self._size = 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._chunks.append(data)
        self._size += len(data)
        while self._size > self._max and self._chunks:
            drop = self._chunks.popleft()
            self._size -= len(drop)
            slack = self._max - self._size
            if slack > 0 and drop:
                keep = drop[-slack:]
                self._chunks.appendleft(keep)
                self._size += len(keep)

    def tail(self, n: int | None = None) -> bytes:
        if n is None or n >= self._size:
            return b"".join(self._chunks)
        # Walk chunks from the right, stop once we have >= n bytes,
        # so a small-tail read doesn't materialize the whole buffer.
        collected: list[bytes] = []
        remaining = n
        for chunk in reversed(self._chunks):
            if len(chunk) >= remaining:
                collected.append(chunk[-remaining:])
                break
            collected.append(chunk)
            remaining -= len(chunk)
        return b"".join(reversed(collected))


@dataclass
class LoadedClosure:
    name: str
    path: Path
    socket_path: Path
    process: asyncio.subprocess.Process
    client: httpx.AsyncClient
    manifest: ClosureManifest | None = None
    stdout_buf: _RingBuffer = field(default_factory=lambda: _RingBuffer(LOG_BUFFER_BYTES))
    stderr_buf: _RingBuffer = field(default_factory=lambda: _RingBuffer(LOG_BUFFER_BYTES))
    _log_tasks: list[asyncio.Task] = field(default_factory=list)


class ClosureLoader:
    """Manages closure lifecycles: load, proxy, logs, unload."""

    def __init__(self):
        self._closures: dict[str, LoadedClosure] = {}
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)

    # ── load / unload ────────────────────────────────────────────

    async def load(self, namespace: str) -> LoadedClosure:
        """Spawn a closure process from /mnt/<namespace>/entry/bin/start.

        The deployment has mounted the closure's /nix volume at
        `/mnt/<namespace>`. The loader finds the start binary, prepends
        the entry dir's bin to PATH, hands the socket path via env, and forks.
        """
        if namespace in self._closures:
            logger.warning("Closure '%s' already loaded, unloading first", namespace)
            await self.unload(namespace)

        mount = CLOSURE_MOUNT_ROOT / namespace
        entry = mount / "entry"
        start_bin = entry / "bin" / "start"
        if not start_bin.exists() or not os.access(start_bin, os.X_OK):
            raise FileNotFoundError(
                f"No executable at {start_bin}. Deployment must mount the closure at {mount} "
                f"with an entry/bin/start before it starts."
            )

        socket_path = SOCKET_DIR / f"{namespace}.sock"
        if socket_path.exists():
            socket_path.unlink()

        # Closure convention: service is callable with no CLI args; it reads
        # AGENTIX_SOCKET from env. PATH is prepped with the closure's own
        # bin/ first, so the service's shell-outs (git, rg, node, ...)
        # resolve to its bundled tools.
        base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env = _scrubbed_env({
            "PATH": f"{entry}/bin:{base_path}",
            "AGENTIX_SOCKET": str(socket_path),
        })

        logger.info("Loading closure '%s' (start=%s)", namespace, start_bin)
        proc = await asyncio.create_subprocess_exec(
            str(start_bin),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        closure = LoadedClosure(
            name=namespace,
            path=mount,
            socket_path=socket_path,
            process=proc,
            client=None,  # set below
        )

        async def _drain(stream: asyncio.StreamReader, buf: _RingBuffer, tag: str) -> None:
            try:
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        break
                    buf.write(chunk)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("log drain (%s/%s) ended: %s", namespace, tag, exc)

        closure._log_tasks.append(asyncio.create_task(_drain(proc.stdout, closure.stdout_buf, "stdout")))
        closure._log_tasks.append(asyncio.create_task(_drain(proc.stderr, closure.stderr_buf, "stderr")))

        # Wait for the socket to appear
        for _ in range(100):  # ~10s
            if socket_path.exists():
                break
            if proc.returncode is not None:
                await self._cleanup(closure)
                raise RuntimeError(
                    f"Closure '{namespace}' exited before creating socket: "
                    f"stderr={closure.stderr_buf.tail(2048).decode(errors='replace')}"
                )
            await asyncio.sleep(0.1)
        else:
            await self._cleanup(closure)
            raise TimeoutError(f"Closure '{namespace}' did not create socket within 10s")

        transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
        closure.client = httpx.AsyncClient(transport=transport, base_url="http://closure", timeout=None)

        # Manifest probe (also a readiness check)
        manifest_raw: dict | None = None
        for _ in range(50):  # ~5s
            try:
                r = await closure.client.get("/")
                if r.status_code < 500:
                    try:
                        manifest_raw = r.json()
                    except Exception:
                        manifest_raw = None
                    break
            except (httpx.ConnectError, httpx.ReadError):
                await asyncio.sleep(0.1)
        else:
            await self._cleanup(closure)
            raise TimeoutError(f"Closure '{namespace}' not responding on socket")

        if isinstance(manifest_raw, dict):
            try:
                closure.manifest = ClosureManifest.model_validate(manifest_raw)
            except ValidationError as exc:
                logger.warning("Closure '%s' manifest failed validation (kept as-is): %s", namespace, exc)
                closure.manifest = ClosureManifest(
                    name=manifest_raw.get("name", namespace),
                    version=manifest_raw.get("version", "0.0.0"),
                    description=manifest_raw.get("description"),
                    kind=manifest_raw.get("kind"),
                )

        self._closures[namespace] = closure
        logger.info("Closure '%s' loaded (manifest=%s)", namespace, closure.manifest)
        return closure

    async def unload(self, name: str) -> None:
        closure = self._closures.pop(name, None)
        if not closure:
            return
        logger.info("Unloading closure '%s'", name)
        await self._cleanup(closure)

    async def _cleanup(self, closure: LoadedClosure) -> None:
        if closure.client is not None:
            try:
                await closure.client.aclose()
            except Exception:  # pragma: no cover
                pass
        if closure.process.returncode is None:
            closure.process.terminate()
            try:
                await asyncio.wait_for(closure.process.wait(), timeout=5)
            except TimeoutError:
                closure.process.kill()
                await closure.process.wait()
        for t in closure._log_tasks:
            t.cancel()
        await asyncio.gather(*closure._log_tasks, return_exceptions=True)
        if closure.socket_path.exists():
            try:
                closure.socket_path.unlink()
            except FileNotFoundError:
                pass

    # ── proxy ────────────────────────────────────────────────────

    def get(self, name: str) -> LoadedClosure:
        closure = self._closures.get(name)
        if not closure:
            raise KeyError(f"Closure not loaded: {name}")
        return closure

    async def proxy_stream(
        self,
        name: str,
        method: str,
        path: str,
        headers: dict,
        body: bytes | None,
        query: str | None = None,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes], Any]:
        """Forward a request and return (status, headers, byte_iterator, closer).

        The caller must `await closer()` once it's done streaming.
        """
        closure = self.get(name)
        url = path + (f"?{query}" if query else "")
        forwarded_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() not in {"host", "transfer-encoding", "content-length"}
        }

        req = closure.client.build_request(
            method=method, url=url, content=body, headers=forwarded_headers
        )
        resp = await closure.client.send(req, stream=True)

        async def _iter() -> AsyncIterator[bytes]:
            async for chunk in resp.aiter_raw():
                yield chunk

        async def _close() -> None:
            await resp.aclose()

        # Strip hop-by-hop headers from response
        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
        }
        return resp.status_code, out_headers, _iter(), _close

    # ── logs & listing ───────────────────────────────────────────

    def logs(self, name: str, tail: int | None = None) -> tuple[str, str]:
        closure = self.get(name)
        stdout = closure.stdout_buf.tail(tail).decode(errors="replace")
        stderr = closure.stderr_buf.tail(tail).decode(errors="replace")
        return stdout, stderr

    def list_closures(self) -> list[ClosureInfo]:
        return [
            ClosureInfo(
                name=name,
                path=str(c.path),
                pid=c.process.pid,
                socket=str(c.socket_path),
                manifest=c.manifest,
            )
            for name, c in self._closures.items()
        ]

    async def shutdown(self) -> None:
        for name in list(self._closures.keys()):
            await self.unload(name)
