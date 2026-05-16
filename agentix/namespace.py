"""Method discovery for namespace targets.

A namespace is whatever object the entry point points at — typically a
Python package (`agentix.bash`), but the framework accepts any object
that has public async / async-generator callables as attributes. There
is no required base class or Protocol; discovery is duck-typed.

```python
# src/agentix/bash/__init__.py
async def run(command: str) -> BashResult:
    ...

async def run_stream(command: str) -> AsyncIterator[BashEvent]:
    ...

# Optional types / constants / private helpers — ignored by the framework
DEFAULT_TIMEOUT = 30
class BashResult(BaseModel): ...
def _helper(): ...
```

The entry point points at the package:

```toml
[project.entry-points."agentix.namespace"]
bash = "agentix.bash"
```

The framework imports the package and `discover_methods` walks its
top-level attributes for `async def` / `async def ... yield`
functions. Constants, types, classes, and `_private` names are
skipped.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any


def discover_methods(target: Any) -> Iterator[tuple[str, Any]]:
    """Yield `(name, function)` for each remote-callable attribute on `target`.

    `target` can be a module (the recommended namespace shape — the
    package itself), a class (for class-style namespaces), or any
    object exposing attributes via `vars()`.

    Discovery rules differ by shape because module top levels are
    cluttered with imports (`dataclass`, `Field`, helper types) that
    shouldn't be remote-callable:

      * **Module target:** keep public **async** functions / async
        generators only. Sync imports (`dataclass`, `Field`, etc.) and
        classes/constants are skipped.
      * **Class target:** keep any public function (sync or async,
        plus `@staticmethod` wrappers). Classes are explicit
        namespaces — every method on them is intentional.

    Names starting with `_` and names listed in
    `target.__namespace_excluded__` are skipped in both cases. For
    class targets the MRO is walked (skipping `object`) and subclass
    overrides take priority.
    """
    excluded = frozenset(getattr(target, "__namespace_excluded__", frozenset()))
    seen: set[str] = set()
    is_module = inspect.ismodule(target)

    if inspect.isclass(target):
        sources = [k for k in target.__mro__ if k is not object]
    else:
        sources = [target]

    for src in sources:
        for name, value in vars(src).items():
            if name in seen or name in excluded or name.startswith("_"):
                continue
            fn = value.__func__ if isinstance(value, staticmethod) else value
            is_async = inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)
            is_sync_fn = inspect.isfunction(fn) and not is_async
            if is_async or (is_sync_fn and not is_module):
                seen.add(name)
                yield name, fn


__all__ = ["discover_methods"]
