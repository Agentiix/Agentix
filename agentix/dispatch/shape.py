"""Call-shape detection — `unary` / `stream` / `bidi`.

The framework's three call shapes are exhaustive; adding a fourth means
editing `detect_shape` plus the matching branches in `Dispatcher` /
`RuntimeClient`. No plugin extension hook — by design.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Literal

from agentix.runtime.shared.rpc import is_channel_annotation

Shape = Literal["unary", "stream", "bidi"]
"""How a method's signature maps onto the wire:

  * `unary`  — plain `T` return; one request, one response
  * `stream` — `async def f(...) -> AsyncIterator[T]: yield ...`
  * `bidi`   — same as stream + one `Channel[U]` parameter
"""


def detect_shape(fn: Callable[..., Any], sig: inspect.Signature | None = None) -> Shape:
    """Derive the wire shape from a function and its signature.

    The runtime check (`inspect.isasyncgenfunction`) is the source of
    truth — an `async def ... yield` body is a real async generator, and
    we trust that over annotations alone (which can be wrong: a regular
    `async def f() -> AsyncIterator[T]: return some_iter` returns a
    coroutine, not a stream).

    Bidi is detected by a `Channel[T]` parameter in addition to async-gen
    return. `AsyncIterator[T]` as a parameter no longer marks bidi —
    `Channel[T]` is now the explicit, type-safe marker.
    """
    if sig is None:
        sig = inspect.signature(fn, eval_str=True)
    if not inspect.isasyncgenfunction(fn):
        return "unary"
    has_channel = any(
        is_channel_annotation(p.annotation) for p in sig.parameters.values()
    )
    return "bidi" if has_channel else "stream"


__all__ = ["Shape", "detect_shape"]
