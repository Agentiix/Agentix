"""agentix — remote calls for sandboxed Python modules.

Integration wheels may contribute modules under `agentix.<short>`
(e.g. `agentix.bash`). Extending `agentix.__path__` lets those modules
co-exist with the framework modules in this package.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from agentix import trace
from agentix.deployment.base import (
    Deployment,
    Sandbox,
    SandboxConfig,
    SandboxId,
    SandboxInfo,
    load_deployment,
    register_deployment,
    session,
)
from agentix.runtime.client import RemoteCallError, RuntimeClient

__version__ = "0.2.0"

__all__ = [
    "Deployment",
    "RemoteCallError",
    "RuntimeClient",
    "Sandbox",
    "SandboxConfig",
    "SandboxId",
    "SandboxInfo",
    "__version__",
    "load_deployment",
    "register_deployment",
    "session",
    "trace",
]
