"""Docker deployment: sandbox CRUD via local Docker.

Design (modular Nix-closure composition):

  Every closure image declares `VOLUME /nix` and ships:
      /nix/store/<hash>-*/    — content-addressed Nix deps
      /nix/entry/bin/start    — no-arg entry point, reads AGENTIX_SOCKET
                                from env

  Deployment's responsibility per unique closure image (cached):
      docker run --rm -v agentix-closure-<key>:/nix <image> true
      A fresh named volume mounted at /nix (the image declares VOLUME /nix)
      is auto-populated by Docker from image-layer content on first attach;
      subsequent calls are no-ops (Docker's own idempotency).

  Sandbox create:
      docker run --name <sid> \\
         -v agentix-closure-<runtime-key>:/mnt/runtime:ro \\
         -v agentix-closure-<claude-key>:/mnt/claude:ro \\
         -v agentix-closure-<swebench-key>:/mnt/swebench:ro \\
         --tmpfs /nix:exec,mode=755 \\
         <task-image> sh -c '<entrypoint>'

  Sandbox entrypoint (inlined):
      mkdir -p /nix/store
      for d in /mnt/*/store; do ln -sfn "$d"/* /nix/store/; done
      exec /mnt/runtime/entry/bin/start

  Runtime on startup scans /mnt/*/entry/bin/start, spawns each one as
  a closure subprocess. No dynamic /load; sandbox contents are fixed at
  create time.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from uuid import uuid4

import httpx

from agentix.deployment.base import Deployment
from agentix.models import SandboxConfig, SandboxInfo

logger = logging.getLogger("agentix.deployment.docker")


async def _docker(*args: str, check: bool = True) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    rc = proc.returncode or 0
    if check and rc != 0:
        raise RuntimeError(f"docker {args[0]} failed: {stderr.decode(errors='replace')}")
    return rc, stdout, stderr


@dataclass
class _SandboxState:
    port: int
    config: SandboxConfig


class DockerDeployment(Deployment):
    """Sandbox CRUD via local Docker."""

    def __init__(self, host_port_start: int = 18000):
        self._next_port = host_port_start
        self._port_lock = asyncio.Lock()
        self._sandboxes: dict[str, _SandboxState] = {}
        self._populated: dict[str, str] = {}  # image ref → named volume
        self._populate_lock = asyncio.Lock()

    # ── port ─────────────────────────────────────────────────────

    async def _allocate_port(self) -> int:
        async with self._port_lock:
            while True:
                port = self._next_port
                self._next_port += 1
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    if s.connect_ex(("127.0.0.1", port)) != 0:
                        return port

    # ── populate one closure image into its named volume ────────

    @staticmethod
    async def _image_digest(image: str) -> str:
        """Resolve a Docker image ref to its content digest (sha256 of the
        image config). Content-addressed, so two refs pointing to identical
        bytes yield the same digest.
        """
        _, stdout, _ = await _docker("inspect", image, "--format", "{{.Id}}")
        raw = stdout.decode().strip()
        # `.Id` is "sha256:<hex>" — keep the hex only.
        return raw.removeprefix("sha256:")[:16]

    async def _ensure_populated(self, image: str) -> str:
        """Ensure the per-content volume `agentix-closure-<digest>` is
        populated from `image`'s /nix. Keyed by image digest (not ref), so:

          * rebuilding an image under the same tag → new digest → new volume
            (no stale content)
          * two refs pointing at the same bytes → same digest → one volume
            (no duplication)

        Docker's volume-init-from-image rule fills a fresh volume from the
        image layer on first attach; if the volume already has content it
        skips — idempotent and cross-process-safe.
        """
        digest = await self._image_digest(image)
        vol = f"agentix-closure-{digest}"

        if self._populated.get(image) == vol:
            return vol

        async with self._populate_lock:
            if self._populated.get(image) == vol:
                return vol
            await _docker("run", "--rm", "-v", f"{vol}:/nix", image, "true")
            self._populated[image] = vol
            logger.info("Populated closure volume '%s' from '%s'", vol, image)
            return vol

    # ── create ───────────────────────────────────────────────────

    async def create(self, config: SandboxConfig) -> SandboxInfo:
        if "runtime" in config.closures:
            raise ValueError("namespace 'runtime' is reserved for config.runtime")

        sandbox_id = f"agentix-{uuid4().hex[:8]}"
        port = await self._allocate_port()

        # Populate all closures in parallel (cached after first).
        pairs: list[tuple[str, str]] = [("runtime", config.runtime)]
        pairs.extend(config.closures.items())
        vols = await asyncio.gather(*(self._ensure_populated(img) for _, img in pairs))

        mount_args: list[str] = []
        for (ns, _image), vol in zip(pairs, vols):
            mount_args.extend(["-v", f"{vol}:/mnt/{ns}:ro"])

        env_args: list[str] = ["-e", f"AGENTIX_BIND_PORT={port}"]
        if config.env:
            for k, v in config.env.items():
                env_args.extend(["-e", f"{k}={v}"])

        entrypoint = (
            "set -e; "
            "mkdir -p /nix/store; "
            "for d in /mnt/*/store; do ln -sfn \"$d\"/* /nix/store/; done; "
            "exec /mnt/runtime/entry/bin/start"
        )

        await _docker(
            "run", "-d",
            "--name", sandbox_id,
            "--network", "host",
            *mount_args,
            "--tmpfs", "/nix:exec,mode=755",
            *env_args,
            config.image,
            "sh", "-c", entrypoint,
        )

        info = SandboxInfo(
            sandbox_id=sandbox_id,
            runtime_url=f"http://localhost:{port}",
            status="running",
        )
        self._sandboxes[sandbox_id] = _SandboxState(port=port, config=config)
        logger.info("Created sandbox %s on port %d", sandbox_id, port)

        await self._wait_healthy(port)
        return info

    async def _wait_healthy(self, port: int) -> None:
        base_url = f"http://localhost:{port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            for _ in range(120):
                try:
                    r = await client.get("/health")
                    if r.status_code == 200:
                        return
                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError(f"Runtime server not alive at {base_url}")

    # ── get / update / delete ────────────────────────────────────

    async def get(self, sandbox_id: str) -> SandboxInfo:
        state = self._sandboxes.get(sandbox_id)
        if state is None:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        rc, stdout, _ = await _docker(
            "inspect", "-f", "{{.State.Status}}", sandbox_id, check=False,
        )
        status = stdout.decode().strip() if rc == 0 else "unknown"
        return SandboxInfo(
            sandbox_id=sandbox_id,
            runtime_url=f"http://localhost:{state.port}",
            status=status,
        )

    async def update(
        self,
        sandbox_id: str,
        config: SandboxConfig,
        *,
        force_recreate: bool = False,
    ) -> SandboxInfo:
        state = self._sandboxes.get(sandbox_id)
        if state is None:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        if force_recreate or config != state.config:
            await self.delete(sandbox_id)
            return await self.create(config)
        return await self.get(sandbox_id)

    async def delete(self, sandbox_id: str) -> None:
        await _docker("rm", "-f", sandbox_id, check=False)
        self._sandboxes.pop(sandbox_id, None)
        logger.info("Deleted sandbox %s", sandbox_id)

    def active_sandboxes(self) -> list[str]:
        return list(self._sandboxes.keys())
