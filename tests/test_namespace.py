"""Unit tests for `Namespace`, `WirePattern`, and `Dispatcher.bind_namespace`.

These are the R1 (dynamic bind + static typing) + R2 (extensible wire
patterns) primitives. Closure-protocol-level integration is exercised
in `test_closure_protocol.py`; here we test the abstractions in
isolation.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest

from agentix.dispatch import Dispatcher
from agentix.namespace import Namespace, discover_methods
from agentix.runtime.models import RemoteRequest
from agentix.wire import (
    BidiPattern,
    StreamPattern,
    UnaryPattern,
    WirePattern,
    _reset_patterns,
    register_pattern,
    select_pattern,
)

# ── Namespace method discovery ──────────────────────────────────────


def test_namespace_methods_only_lists_public_callables() -> None:
    class N(Namespace):
        def public(self, x: int) -> int: ...
        def _private(self) -> None: ...  # underscore → skipped
        constant = 42  # non-function → skipped

    names = [n for n, _ in discover_methods(N)]
    assert names == ["public"]


def test_namespace_excluded_hides_methods() -> None:
    class N(Namespace):
        __namespace_excluded__ = frozenset({"hidden"})

        def visible(self) -> None: ...
        def hidden(self) -> None: ...

    assert [n for n, _ in discover_methods(N)] == ["visible"]


def test_namespace_inherits_methods_from_namespace_ancestors() -> None:
    """A Namespace subclass may inherit methods from another Namespace
    (e.g. for shared-mixin stubs). The composition rule applies to stub↔impl,
    not stub↔stub."""

    class Base(Namespace):
        def common(self) -> int: ...

    class Extended(Base):
        def extra(self) -> str: ...

    names = sorted(n for n, _ in discover_methods(Extended))
    assert names == ["common", "extra"]


# ── Pattern selection ───────────────────────────────────────────────


def _sig(fn: object) -> inspect.Signature:
    # eval_str=True mirrors what Dispatcher.bind does — resolve PEP 563
    # stringified annotations so `get_origin(AsyncIterator[T])` works.
    return inspect.signature(fn, eval_str=True)  # type: ignore[arg-type]


def test_select_unary_for_plain_signature() -> None:
    def f(x: int) -> str: ...
    assert select_pattern(_sig(f)) is UnaryPattern


def test_select_stream_for_async_iterator_return() -> None:
    def f(x: int) -> AsyncIterator[int]: ...
    assert select_pattern(_sig(f)) is StreamPattern


def test_select_bidi_for_async_iterator_param_and_return() -> None:
    def f(events: AsyncIterator[str]) -> AsyncIterator[int]: ...
    assert select_pattern(_sig(f)) is BidiPattern


# ── register_pattern() — third-party extensibility (R2) ────────────


def test_register_pattern_prepends_and_overrides_builtins() -> None:
    """A user pattern with a stricter `matches` outranks the built-ins."""

    class StringStreamPattern(WirePattern):
        name = "string-stream"

        @classmethod
        def matches(cls, sig: inspect.Signature) -> bool:
            ret = sig.return_annotation
            return getattr(ret, "__origin__", None) is __import__(
                "collections.abc",
            ).abc.AsyncIterator and getattr(ret, "__args__", (None,))[0] is str

        def bind(self, sig: inspect.Signature) -> None:
            return

    try:
        register_pattern(StringStreamPattern)

        def stream_str() -> AsyncIterator[str]: ...
        def stream_int() -> AsyncIterator[int]: ...

        assert select_pattern(_sig(stream_str)) is StringStreamPattern
        # int stream still falls through to the built-in StreamPattern
        assert select_pattern(_sig(stream_int)) is StreamPattern
    finally:
        _reset_patterns()


# ── Dispatcher.bind_namespace ───────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_namespace_routes_through_dispatcher() -> None:
    """A full Namespace round-trip: stub class + independent impl class →
    dispatcher. The impl does NOT inherit from the stub — composition."""

    class Math(Namespace):
        async def add(self, a: int, b: int) -> int: ...
        async def echo(self, items: list[str]) -> list[str]: ...

    class MathImpl:  # no inheritance from Math
        async def add(self, a: int, b: int) -> int:
            return a + b

        async def echo(self, items: list[str]) -> list[str]:
            return list(reversed(items))

    d = Dispatcher().bind_namespace(Math, MathImpl())
    assert set(d.methods()) == {"add", "echo"}

    resp = await d.dispatch(RemoteRequest(
        package="x", method="add", args=[], kwargs={"a": 2, "b": 3},
    ))
    assert resp.ok and resp.value == 5

    resp = await d.dispatch(RemoteRequest(
        package="x", method="echo", args=[], kwargs={"items": ["a", "b", "c"]},
    ))
    assert resp.ok and resp.value == ["c", "b", "a"]


@pytest.mark.asyncio
async def test_bind_namespace_rejects_impl_missing_methods() -> None:
    class Stub(Namespace):
        def run(self) -> int: ...
        def other(self) -> str: ...

    class Partial:
        def run(self) -> int:
            return 0
        # `other` deliberately missing

    with pytest.raises(TypeError, match="is missing method"):
        Dispatcher().bind_namespace(Stub, Partial())


@pytest.mark.asyncio
async def test_bind_namespace_picks_correct_pattern() -> None:
    class N(Namespace):
        async def unary(self, x: int) -> int: ...
        async def stream(self, n: int) -> AsyncIterator[int]: ...
        async def bidi(self, events: AsyncIterator[str]) -> AsyncIterator[int]: ...

    class NImpl:  # composition — independent of N
        async def unary(self, x: int) -> int:
            return x

        async def stream(self, n: int) -> AsyncIterator[int]:
            for i in range(n):
                yield i

        async def bidi(self, events: AsyncIterator[str]) -> AsyncIterator[int]:
            async for e in events:
                yield len(e)

    d = Dispatcher().bind_namespace(N, NImpl())
    assert d.is_streaming("unary") is False
    assert d.is_streaming("stream") is True
    assert d.is_bidi("stream") is False
    assert d.is_bidi("bidi") is True


@pytest.mark.asyncio
async def test_bind_namespace_works_with_protocol_typed_impl() -> None:
    """If the user opts into Protocol typing, pyright structurally verifies
    the impl satisfies the stub. The framework itself doesn't require it."""
    from typing import Protocol, runtime_checkable

    @runtime_checkable
    class Greeting(Namespace, Protocol):
        async def hello(self, name: str) -> str: ...

    class GreetingImpl:
        async def hello(self, name: str) -> str:
            return f"hi {name}"

        async def _internal(self) -> None: ...  # private — not part of ABI

    impl: Greeting = GreetingImpl()  # pyright would catch a structural mismatch
    d = Dispatcher().bind_namespace(Greeting, impl)
    assert d.methods() == ["hello"]
    resp = await d.dispatch(RemoteRequest(
        package="x", method="hello", args=[], kwargs={"name": "alice"},
    ))
    assert resp.ok and resp.value == "hi alice"


# ── Auto-discovery (no _register.py) ────────────────────────────────


@pytest.mark.asyncio
async def test_auto_discover_finds_unique_namespace_pair(tmp_path, monkeypatch) -> None:
    """No `_register.py` → runtime infers stub + impl by convention."""
    import sys
    import textwrap

    pkg_root = tmp_path / "agentix_closures" / "autodisc"
    pkg_root.mkdir(parents=True)
    (pkg_root / "__init__.py").write_text(textwrap.dedent("""
        from agentix.namespace import Namespace
        class Math(Namespace):
            async def add(self, a: int, b: int) -> int: ...
    """))
    (pkg_root / "_impl.py").write_text(textwrap.dedent("""
        class MathImpl:
            async def add(self, a: int, b: int) -> int:
                return a + b
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    from agentix.dispatch import _import_and_register
    from agentix.models import ClosureManifest
    manifest = ClosureManifest(
        abi=1, name="autodisc", version="0.0.1",
        package="agentix_closures.autodisc",
    )
    try:
        d = _import_and_register(manifest)
        assert d.methods() == ["add"]
        resp = await d.dispatch(RemoteRequest(
            package="x", method="add", args=[], kwargs={"a": 4, "b": 5},
        ))
        assert resp.ok and resp.value == 9
    finally:
        sys.modules.pop("agentix_closures.autodisc", None)
        sys.modules.pop("agentix_closures.autodisc._impl", None)


def test_auto_discover_rejects_zero_namespaces(tmp_path, monkeypatch) -> None:
    import sys

    pkg_root = tmp_path / "agentix_closures" / "empty"
    pkg_root.mkdir(parents=True)
    (pkg_root / "__init__.py").write_text("# no Namespace here\n")
    (pkg_root / "_impl.py").write_text("# nothing\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    from agentix.dispatch import _import_and_register
    from agentix.models import ClosureManifest
    manifest = ClosureManifest(
        abi=1, name="empty", version="0.0.1",
        package="agentix_closures.empty",
    )
    try:
        with pytest.raises(TypeError, match="no Namespace subclass"):
            _import_and_register(manifest)
    finally:
        sys.modules.pop("agentix_closures.empty", None)


def test_auto_discover_rejects_missing_impl_class(tmp_path, monkeypatch) -> None:
    import sys
    import textwrap

    pkg_root = tmp_path / "agentix_closures" / "noimpl"
    pkg_root.mkdir(parents=True)
    (pkg_root / "__init__.py").write_text(textwrap.dedent("""
        from agentix.namespace import Namespace
        class Greet(Namespace):
            async def hi(self) -> str: ...
    """))
    (pkg_root / "_impl.py").write_text("# missing GreetImpl\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    from agentix.dispatch import _import_and_register
    from agentix.models import ClosureManifest
    manifest = ClosureManifest(
        abi=1, name="noimpl", version="0.0.1",
        package="agentix_closures.noimpl",
    )
    try:
        with pytest.raises(TypeError, match="GreetImpl"):
            _import_and_register(manifest)
    finally:
        sys.modules.pop("agentix_closures.noimpl", None)
        sys.modules.pop("agentix_closures.noimpl._impl", None)
