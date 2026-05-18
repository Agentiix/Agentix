"""`FunctionInvoker` — lazy-binding calls into a Python module or class.

Construct with a target (any object holding async functions as
attributes). `call_unary` / `call_stream` / `call_bidi` look up
`request.function` on the target, lazily build a `_BoundMethod` for it
(TypeAdapter compile happens once per method, cached), coerce wire args
through the adapters, await the impl, serialize the result, and trap
exceptions into a `RemoteError` so the wire stays 200.

The worker subprocess constructs one `FunctionInvoker` per imported
module and reuses it for later calls to the same module.
"""

from __future__ import annotations

import inspect
import logging
import traceback
from collections.abc import AsyncIterator
from typing import Any, get_args

from pydantic import TypeAdapter, ValidationError

from agentix.invoke.bound import _BoundMethod, coerce_args
from agentix.invoke.shape import detect_shape
from agentix.runtime.shared.idents import FunctionName
from agentix.runtime.shared.models import (
    RemoteError,
    RemoteRequest,
    RemoteResponse,
)
from agentix.runtime.shared.rpc import is_channel_annotation

logger = logging.getLogger("agentix.invoke")


class FunctionInvoker:
    """Call public functions exposed by a Python target.

    `target` is any object exposing async functions / async generators
    as attributes — a module (recommended), a class, a regular object.
    Functions are resolved and bound on first call by name; results cached.
    """

    def __init__(self, target: Any) -> None:
        self._target = target
        self._methods: dict[FunctionName, _BoundMethod[Any, Any]] = {}  # lazy cache

    # ── lazy resolution ────────────────────────────────────────────

    def _resolve(self, function: FunctionName) -> _BoundMethod[Any, Any] | None:
        """Look up + lazy-bind. Returns None for missing attributes or
        Python dunders.

        Any callable attribute is remote-callable. Sync functions work for
        unary; the invoker checks `isawaitable(result)` at runtime.
        Streams / bidi structurally need async generators (`async for`
        is the only way to iterate them) — `detect_shape` enforces that
        via `isasyncgenfunction`.
        """
        cached = self._methods.get(function)
        if cached is not None:
            return cached
        if not isinstance(function, str):
            return None
        # Block Python dunders — they're framework machinery, never user methods.
        if function.startswith("__") and function.endswith("__"):
            return None
        fn = getattr(self._target, function, None)
        if fn is None:
            return None
        # @staticmethod wrappers — unwrap to the underlying function so
        # `detect_shape`'s checks see the real function.
        actual = fn.__func__ if isinstance(fn, staticmethod) else fn
        if not callable(actual):
            return None
        bound = self._build(function, actual)
        self._methods[function] = bound
        return bound

    def _build(self, name: FunctionName, fn: Any) -> _BoundMethod[Any, Any]:
        # eval_str=True resolves PEP 563 stringified annotations
        # (`from __future__ import annotations` in the module) — without
        # it, `param.annotation` would be a string and `get_origin` would
        # return None, mis-classifying streams as unary.
        sig = inspect.signature(fn, eval_str=True)
        shape = detect_shape(fn, sig)

        param_adapters: dict[str, TypeAdapter[Any]] = {}
        channel_params: list[tuple[str, Any]] = []
        for pname, param in sig.parameters.items():
            ann = param.annotation if param.annotation is not inspect.Parameter.empty else Any
            if is_channel_annotation(ann):
                # Channel[T] params: adapter validates items, not the channel itself.
                args = get_args(ann)
                item_type = args[0] if args else Any
                channel_params.append((pname, item_type))
                param_adapters[pname] = TypeAdapter(item_type)
            else:
                param_adapters[pname] = TypeAdapter(ann)

        return_ann = sig.return_annotation if sig.return_annotation is not inspect.Signature.empty else Any
        item_adapter: TypeAdapter[Any] | None = None
        input_channel_param: str | None = None
        input_item_adapter: TypeAdapter[Any] | None = None
        if shape == "unary":
            return_adapter = TypeAdapter(return_ann)
        else:
            # Stream + bidi both serialise items via the return type's T.
            args = get_args(return_ann)
            item_type = args[0] if args else Any
            item_adapter = TypeAdapter(item_type)
            return_adapter = TypeAdapter(Any)  # unused on streaming path
            if shape == "bidi":
                input_channel_param, input_item_type = channel_params[0]
                input_item_adapter = TypeAdapter(input_item_type)

        return _BoundMethod(
            name=name,
            stub=fn,
            impl=fn,
            signature=sig,
            shape=shape,
            param_adapters=param_adapters,
            return_adapter=return_adapter,
            item_adapter=item_adapter,
            input_channel_param=input_channel_param,
            input_item_adapter=input_item_adapter,
        )

    # ── introspection (used by the in-process worker) ─────────────

    def is_streaming(self, function: FunctionName) -> bool:
        m = self._resolve(function)
        return m is not None and m.is_stream

    def is_bidi(self, function: FunctionName) -> bool:
        m = self._resolve(function)
        return m is not None and m.is_bidi

    def input_adapter_for(self, function: FunctionName) -> TypeAdapter[Any] | None:
        m = self._resolve(function)
        return m.input_item_adapter if m else None

    # ── call entry points ─────────────────────────────────────────

    async def call_unary(self, request: RemoteRequest) -> RemoteResponse:
        """Resolve and invoke a unary function; serialize its return value."""
        m = self._resolve(request.function)
        if m is None:
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type="FunctionNotFound",
                    message=f"no public remote function {request.function!r} on {self._target!r}",
                ),
            )
        try:
            args, kwargs = coerce_args(m, request.args, request.kwargs)
        except ValidationError as exc:
            return RemoteResponse(
                ok=False,
                error=RemoteError(type="ValidationError", message=str(exc)),
            )
        try:
            result = m.impl(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.exception("remote function '%s' raised", m.name)
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type=type(exc).__name__,
                    message=str(exc),
                    traceback=traceback.format_exc(),
                ),
            )
        try:
            value = m.return_adapter.dump_python(result, mode="python")
        except Exception as exc:
            return RemoteResponse(
                ok=False,
                error=RemoteError(
                    type="SerializationError",
                    message=f"failed to serialize return value: {exc}",
                ),
            )
        return RemoteResponse(ok=True, value=value)

    async def call_stream(self, request: RemoteRequest) -> AsyncIterator[dict[str, Any]]:
        """Server-streaming call. Yields {item|end|error} event dicts."""
        m = self._resolve(request.function)
        if m is None:
            yield {"type": "error", "error": RemoteError(
                type="FunctionNotFound",
                message=f"no public remote function {request.function!r} on {self._target!r}",
            ).model_dump()}
            return
        if not m.is_stream or m.is_bidi:
            yield {"type": "error", "error": RemoteError(
                type="NotAStreamFunction",
                message=f"function {request.function!r} is not a non-bidi streaming function",
            ).model_dump()}
            return
        try:
            args, kwargs = coerce_args(m, request.args, request.kwargs)
        except ValidationError as exc:
            yield {"type": "error", "error": RemoteError(type="ValidationError", message=str(exc)).model_dump()}
            return
        try:
            result = m.impl(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            assert m.item_adapter is not None
            async for item in result:
                try:
                    value = m.item_adapter.dump_python(item, mode="python")
                except Exception as exc:
                    yield {"type": "error", "error": RemoteError(
                        type="SerializationError",
                        message=f"failed to serialize item: {exc}",
                    ).model_dump()}
                    return
                yield {"type": "item", "value": value}
        except Exception as exc:
            logger.exception("remote stream function '%s' raised mid-stream", m.name)
            yield {"type": "error", "error": RemoteError(
                type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            ).model_dump()}
            return
        yield {"type": "end"}

    async def call_bidi(
        self,
        request: RemoteRequest,
        input_iter: AsyncIterator[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Bidirectional call. `input_iter` is the caller-pushed inbound stream;
        items must already match the impl's `Channel[T]` item type."""
        m = self._resolve(request.function)
        if m is None:
            yield {"type": "error", "error": RemoteError(
                type="FunctionNotFound",
                message=f"no public remote function {request.function!r} on {self._target!r}",
            ).model_dump()}
            return
        if not m.is_bidi:
            yield {"type": "error", "error": RemoteError(
                type="NotABidiFunction",
                message=f"function {request.function!r} is not bidirectional",
            ).model_dump()}
            return
        assert m.input_channel_param is not None
        # Bind non-channel args/kwargs; inject input_iter as the channel param.
        non_channel_kwargs = dict(request.kwargs)
        non_channel_kwargs.pop(m.input_channel_param, None)
        try:
            bound = m.signature.bind_partial(*request.args, **non_channel_kwargs)
            bound.apply_defaults()
            coerced: dict[str, Any] = {}
            for pname, raw in bound.arguments.items():
                if pname == m.input_channel_param:
                    continue
                adapter = m.param_adapters.get(pname)
                coerced[pname] = adapter.validate_python(raw) if adapter is not None else raw
            coerced[m.input_channel_param] = input_iter
        except (TypeError, ValidationError) as exc:
            yield {"type": "error", "error": RemoteError(type=type(exc).__name__, message=str(exc)).model_dump()}
            return
        try:
            result = m.impl(**coerced)
            if inspect.isawaitable(result):
                result = await result
            assert m.item_adapter is not None
            async for item in result:
                try:
                    value = m.item_adapter.dump_python(item, mode="python")
                except Exception as exc:
                    yield {"type": "error", "error": RemoteError(
                        type="SerializationError",
                        message=f"failed to serialize item: {exc}",
                    ).model_dump()}
                    return
                yield {"type": "item", "value": value}
        except Exception as exc:
            logger.exception("remote bidi function '%s' raised mid-stream", m.name)
            yield {"type": "error", "error": RemoteError(
                type=type(exc).__name__,
                message=str(exc),
                traceback=traceback.format_exc(),
            ).model_dump()}
            return
        yield {"type": "end"}


__all__ = ["FunctionInvoker"]
