"""Abstract deployment interface: sandbox CRUD."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agentix.idents import SandboxId
from agentix.models import SandboxConfig, SandboxInfo


@dataclass
class Sandbox:
    """Live sandbox handle — `runtime_url` is what `RuntimeClient` connects to."""

    sandbox_id: SandboxId
    runtime_url: str
    status: str


class Deployment(ABC):
    """Sandbox lifecycle management.

    Each infrastructure backend (Docker, K8s, Modal, ...) implements this
    interface.

    Two ways to use a sandbox:

        # Scoped — deleted on context exit
        async with deployment.session(config) as sandbox:
            ...

        # Manual — caller owns the lifetime
        sandbox = await deployment.create(config)
        try:
            ...
        finally:
            await deployment.delete(sandbox.sandbox_id)
    """

    @abstractmethod
    async def create(self, config: SandboxConfig) -> Sandbox:
        """Create a sandbox. The caller is responsible for calling `delete()`."""

    @abstractmethod
    async def delete(self, sandbox_id: SandboxId) -> None:
        """Destroy a sandbox and release its resources."""

    @abstractmethod
    async def get(self, sandbox_id: SandboxId) -> SandboxInfo:
        """Snapshot of the sandbox's current state."""

    @asynccontextmanager
    async def session(self, config: SandboxConfig) -> AsyncIterator[Sandbox]:
        """Scoped sandbox: created on entry, deleted on exit."""
        sandbox = await self.create(config)
        try:
            yield sandbox
        finally:
            await self.delete(sandbox.sandbox_id)
