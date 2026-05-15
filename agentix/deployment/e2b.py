"""E2B deployment backend — stub.

E2B (https://e2b.dev/) hosts ephemeral sandboxes seeded by their own
"template" image format rather than arbitrary OCI images. The
integration shape will look like:

    E2BDeployment(api_key=..., template_id=...).create(config)

A bundle image therefore needs to be published as an E2B template
first (`e2b template build`) before it can be deployed here. The
class exists today so `agentix deploy e2b` can fail with a clear
error and so callers can write code against the real interface.
"""

from __future__ import annotations

from agentix.deployment.base import Deployment, Sandbox
from agentix.idents import SandboxId
from agentix.models import SandboxConfig, SandboxInfo


class E2BDeployment(Deployment):
    """Sandbox CRUD via E2B (pending integration)."""

    def __init__(
        self,
        api_key: str | None = None,
        template_id: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._template_id = template_id

    async def create(self, config: SandboxConfig) -> Sandbox:  # noqa: ARG002
        raise NotImplementedError(
            "E2BDeployment is not wired yet. E2B's template system means a "
            "bundle image has to be published as a template first; the "
            "build pipeline + API integration are on the deploy roadmap."
        )

    async def delete(self, sandbox_id: SandboxId) -> None:  # noqa: ARG002
        raise NotImplementedError("E2BDeployment.delete: see create()")

    async def get(self, sandbox_id: SandboxId) -> SandboxInfo:  # noqa: ARG002
        raise NotImplementedError("E2BDeployment.get: see create()")
