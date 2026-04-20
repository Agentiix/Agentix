"""Abstract deployment interface: sandbox CRUD."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agentix.models import SandboxConfig, SandboxInfo


class Deployment(ABC):
    """Sandbox lifecycle management.

    Each infrastructure backend (Docker, K8s, Modal, ...) implements this
    interface. The orchestrator doesn't care which one is used.

    The deployment can be used as an async context manager; any sandboxes
    still alive on __aexit__ are deleted.
    """

    @abstractmethod
    async def create(self, config: SandboxConfig) -> SandboxInfo:
        """Create a sandbox.

        Steps (implementation-specific):
        1. Ensure each closure image's /nix content is available (e.g. populated
           into a named volume keyed by image digest).
        2. Mount each closure at `/mnt/<namespace>:ro` and provide a writable
           tmpfs `/nix`; the sandbox entrypoint merges store paths via a
           symlink forest under `/nix/store`.
        3. Exec the agentix runtime server (`/mnt/runtime/entry/bin/start`);
           it scans `/mnt` on startup and forks every closure it finds.

        Returns SandboxInfo with runtime_url for HTTP communication.
        """

    @abstractmethod
    async def get(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox status."""

    @abstractmethod
    async def update(self, sandbox_id: str, config: SandboxConfig,
                     *, force_recreate: bool = False) -> SandboxInfo:
        """Update sandbox config. Attempts in-place update when possible.
        Falls back to recreate when base image or runtime changes, or force_recreate=True."""

    @abstractmethod
    async def delete(self, sandbox_id: str) -> None:
        """Destroy sandbox and release resources."""

    @abstractmethod
    def active_sandboxes(self) -> list[str]:
        """Return the IDs of sandboxes this deployment has created and not yet deleted."""

    async def delete_all(self) -> None:
        """Delete every sandbox still alive under this deployment."""
        for sandbox_id in self.active_sandboxes():
            await self.delete(sandbox_id)

    async def __aenter__(self) -> Deployment:
        return self

    async def __aexit__(self, *args) -> None:
        await self.delete_all()
