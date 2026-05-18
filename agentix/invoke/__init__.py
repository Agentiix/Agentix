"""Server-side function invocation helpers.

`FunctionInvoker(target).call_unary(req)` looks up `req.function` on
`target` and invokes it; functions bind lazily on first call
(TypeAdapter compile is cached per function).

Split into:

  - `shape`       — call-shape detection (`unary` / `stream` / `bidi`)
  - `bound`       — `_BoundMethod` record + arg coercion helper
  - `invoker`     — the `FunctionInvoker` class itself

Internal surface: `FunctionInvoker` and `detect_shape`.
"""

from agentix.invoke.invoker import FunctionInvoker
from agentix.invoke.shape import Shape, detect_shape

__all__ = [
    "FunctionInvoker",
    "Shape",
    "detect_shape",
]
