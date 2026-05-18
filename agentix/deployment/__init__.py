"""Deployment Protocol + backend discovery.

Core ships the `Deployment` Protocol, `Sandbox` dataclass, and backend
registry. Backend wheels (`agentix-deployment-docker`, `-daytona`,
`-e2b`, third-party) each install a sibling module under
`agentix.deployment`; extending `__path__` lets those siblings co-exist
with the framework files in this directory.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from agentix.deployment.base import Deployment

__all__ = ["Deployment"]
