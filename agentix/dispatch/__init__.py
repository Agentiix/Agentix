"""Namespace dispatch — binds public functions for RPC, routes wire requests to impls.

The runtime's multiplexer instantiates one `Dispatcher` per namespace
inside a worker subprocess. Tests bypass the subprocess path via
`Multiplexer._register_inprocess(target)`.

Split into:

  - `shape`         — call-shape detection (`unary` / `stream` / `bidi`)
  - `bound`         — `_BoundMethod` record + arg coercion helper
  - `dispatcher`    — the `Dispatcher` class itself
  - `entry_points`  — `agentix.namespace` entry-point discovery

Public surface (re-exported here) is `Dispatcher`, `Shape`, `detect_shape`,
`NAMESPACE_ENTRY_POINT_GROUP`, and `discover_entry_points`. The split is
internal — `from agentix.dispatch import Dispatcher` keeps working.
"""

from agentix.dispatch.dispatcher import Dispatcher
from agentix.dispatch.entry_points import (
    NAMESPACE_ENTRY_POINT_GROUP,
    discover_entry_points,
)
from agentix.dispatch.shape import Shape, detect_shape

__all__ = [
    "Dispatcher",
    "NAMESPACE_ENTRY_POINT_GROUP",
    "Shape",
    "detect_shape",
    "discover_entry_points",
]
