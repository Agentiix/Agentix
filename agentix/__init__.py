"""agentix — typed remote calls for sandboxed Python modules.

Integration wheels may contribute modules under `agentix.<short>`
(e.g. `agentix.bash`). Extending `agentix.__path__` lets those modules
co-exist with the framework modules in this package.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from agentix.runtime.client import RemoteCallError, RuntimeClient
from agentix.runtime.shared.rpc import Bidi, Channel, RemoteCall, Stream, Unary

__version__ = "0.1.0"

__all__ = [
    "Bidi",
    "Channel",
    "RemoteCall",
    "RemoteCallError",
    "RuntimeClient",
    "Stream",
    "Unary",
    "__version__",
]
