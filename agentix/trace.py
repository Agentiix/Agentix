"""Trace emission for closures.

Closure impls call `agentix.trace.emit(kind, payload)` to record one
event in the rollout's trace. The runtime installs an emitter at startup
that broadcasts every event over the Socket.IO `trace` channel for any
subscribing `RuntimeClient.traces()` consumer.

`call_id` correlates events to a specific rollout (e.g. an RL trajectory
index). The dispatcher pins the active call_id into a contextvar before
invoking each impl, so `emit()` picks it up automatically — closures
don't have to thread call_id through their code.

If no emitter is installed (e.g. the module imported outside an agentix
runtime), `emit()` is a no-op — tracing never breaks a rollout.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable
from typing import Any, Final

from agentix.idents import CallId, PackageName

EmitFn = Callable[[str, dict[str, Any], CallId | None, PackageName | None], None]

_current_call_id: contextvars.ContextVar[CallId | None] = contextvars.ContextVar(
    "agentix_trace_call_id", default=None,
)
_current_source: contextvars.ContextVar[PackageName | None] = contextvars.ContextVar(
    "agentix_trace_source", default=None,
)
_emit_fn: EmitFn | None = None


def _install_emitter(fn: EmitFn) -> None:
    """Wire up the runtime's emit pathway. Called once at server startup."""
    global _emit_fn
    _emit_fn = fn


def _uninstall_emitter() -> None:
    global _emit_fn
    _emit_fn = None


def set_call_context(
    call_id: CallId | None,
    source: PackageName | None,
) -> tuple[contextvars.Token, contextvars.Token]:
    """Set the active call_id + source for trace events emitted while this
    context is on the call stack. Returns the contextvar reset tokens.
    """
    return _current_call_id.set(call_id), _current_source.set(source)


def reset_call_context(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    """Restore the call_id + source contextvars to their previous values."""
    cid_token, src_token = tokens
    _current_call_id.reset(cid_token)
    _current_source.reset(src_token)


def current_call_id() -> CallId | None:
    """The call_id pinned by the dispatcher for the current request, if any."""
    return _current_call_id.get()


def current_source() -> PackageName | None:
    """The closure package currently being dispatched, if any."""
    return _current_source.get()


def emit(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    call_id: CallId | None = None,
    source: PackageName | None = None,
) -> None:
    """Record a single trace event. No-op if tracing isn't enabled (e.g. the
    closure is running outside an agentix runtime).

    `call_id` and `source` default to the dispatcher-set context. Closures
    should normally call `emit("kind", {...})` and let the runtime fill in
    the correlation.
    """
    if _emit_fn is None:
        return
    cid: Final = call_id if call_id is not None else _current_call_id.get()
    src: Final = source if source is not None else _current_source.get()
    try:
        _emit_fn(kind, payload or {}, cid, src)
    except Exception:
        # Tracing must never break a rollout. Swallow.
        pass


def now() -> float:
    """Helper for callers that want to record their own timestamps."""
    return time.time()
