"""Daytona deployment backend — stub.

Daytona (https://www.daytona.io/) runs managed sandboxes from OCI
images. The integration shape will look like:

    DaytonaDeployment(api_key=..., workspace_image=...).create(config)

That's deferred. The class exists today so `agentix deploy daytona`
can fail with a clear error and so callers can write code against the
real interface in advance.
"""

from __future__ import annotations

from agentix.deployment.base import Deployment, Sandbox
from agentix.idents import SandboxId
from agentix.models import SandboxConfig, SandboxInfo


class DaytonaDeployment(Deployment):
    """Sandbox CRUD via Daytona (pending integration)."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    async def create(self, config: SandboxConfig) -> Sandbox:  # noqa: ARG002
        raise NotImplementedError(
            "DaytonaDeployment is not wired yet. The CLI surface exists so "
            "you can plan against it; the Daytona REST integration is the "
            "next item on the deploy roadmap."
        )

    async def delete(self, sandbox_id: SandboxId) -> None:  # noqa: ARG002
        raise NotImplementedError("DaytonaDeployment.delete: see create()")

    async def get(self, sandbox_id: SandboxId) -> SandboxInfo:  # noqa: ARG002
        raise NotImplementedError("DaytonaDeployment.get: see create()")
