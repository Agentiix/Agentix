"""Deployment axis — Protocol + backend-discovery.

`agentix.deployment` is an extensible namespace: core ships the
`Deployment` Protocol + `Sandbox` dataclass + the `_plugin.Registry`
loader; backend wheels (`agentix-deployment-docker`,
`-daytona`, `-e2b`, third-party) each install a single sibling module
into `<site-packages>/agentix/deployment/<backend>.py`. The
`pkgutil.extend_path` line below is what lets those siblings co-exist
with the framework files in this directory.
"""

import pkgutil

__path__ = pkgutil.extend_path(__path__, __name__)

from agentix.deployment.base import Deployment

__all__ = ["Deployment"]
