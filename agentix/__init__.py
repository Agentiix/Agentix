"""agentix — a Nix-closure runtime for Docker sandboxes.

`agentix` is a namespace-extensible regular package. The framework
ships its own subpackages (`agentix.cli`, `agentix.dispatch`,
`agentix.runtime`, …); closures contribute additional subpackages
under reserved kind-roots: `agentix.primitive.<short>`,
`agentix.agent.<short>`, `agentix.dataset.<short>`. The
`pkgutil.extend_path` call below makes `agentix.__path__` aggregate
every `agentix/` directory on `sys.path`, so a closure dist
installing files at `<site-packages>/agentix/primitive/bash/`
becomes importable as `from agentix.primitive.bash import Bash`.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

# `trace` is imported eagerly so closure impls can `from agentix import trace`
# without circular-import gymnastics. It has no runtime deps and registers an
# emitter only when the server boots, so this is cheap.
from agentix import trace
from agentix.deployment.base import Sandbox
from agentix.deployment.docker import DockerDeployment
from agentix.dispatch import Dispatcher, Registry
from agentix.models import SandboxConfig, SandboxInfo
from agentix.rollout import RolloutPool
from agentix.runtime.client import RemoteCallError, RuntimeClient
from agentix.runtime.models import LogRecord, TraceEvent

__version__ = "0.1.0"

__all__ = [
    "Dispatcher",
    "DockerDeployment",
    "LogRecord",
    "Registry",
    "RemoteCallError",
    "RolloutPool",
    "RuntimeClient",
    "Sandbox",
    "SandboxConfig",
    "SandboxInfo",
    "TraceEvent",
    "__version__",
    "trace",
]
