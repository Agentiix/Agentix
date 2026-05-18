"""Runtime callable invocation helpers.

`CallableInvoker().call_unary(fn, req)` validates args against `fn`'s
signature, invokes the callable, and serializes the result.

Split into:

  - `shape`       — declared call-shape detection (`unary` / `stream` / `bidi`)
  - `bound`       — `_BoundCallable` record + arg coercion helper
  - `invoker`     — the `CallableInvoker` class itself

Internal surface: `CallableInvoker` and `detect_declared_shape`.
"""

from agentix.runtime.invoke.invoker import CallableInvoker
from agentix.runtime.invoke.shape import Shape, detect_declared_shape

__all__ = [
    "CallableInvoker",
    "Shape",
    "detect_declared_shape",
]
