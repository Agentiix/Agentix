"""Shared closure-spec resolution for `agentix build` / `agentix install`.

A *spec* is whatever the user types on the command line — a short name
(`bash`), a relative path (`./primitives/bash`), or an image reference
(`docker.io/me/agent:0.1.0`). This module turns one of those into a
`ClosureSpec` whose `kind` tells the caller how to stage it.

Resolution order, per spec:

  1. **Path** — anything starting with `.` or `/`, or any directory
     containing `pyproject.toml`, is used as-is.
  2. **Image ref** — strings with both `/` and `:`, not starting with
     `.`/`/`. Currently surfaces as `kind="image"`; downstream callers
     decide whether they support image-ref sourcing.
  3. **Short name** — searched against the conventional extension roots
     in the repo (`primitives/<name>`, `agents/<name>`, `datasets/<name>`).
     If no local match, falls back to PyPI as
     `agentix-<kind>-<name>` for each kind.

PyPI is best-effort: this module records the candidate dist name(s)
without fetching anything. The actual `pip download` + wheel unpack
happens at build/install time and is allowed to fail loudly.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_ROOTS = ("primitives", "agents", "datasets")
CLOSURE_KINDS = ("primitive", "agent", "dataset")


@dataclass
class ClosureSpec:
    """One resolved input to a build / install. Exactly one source field is set."""

    short: str
    kind: Literal["path", "pypi", "image"]
    path: Path | None = None
    pypi_dist: str | None = None
    image_ref: str | None = None


def _looks_like_image_ref(spec: str) -> bool:
    """Heuristic: `name/something:tag` is an image ref; `./foo` or bare
    names are not. Conservative — bare `:` without a `/` is ambiguous and
    treated as a name."""
    return "/" in spec and ":" in spec and not spec.startswith((".", "/"))


def _looks_like_path(spec: str) -> bool:
    if spec.startswith((".", "/")):
        return True
    p = Path(spec)
    return p.is_dir() and (p / "pyproject.toml").is_file()


def _find_local(name: str) -> Path | None:
    """Look up `<root>/<name>/` under each extension root in the repo."""
    for root in EXTENSION_ROOTS:
        candidate = REPO_ROOT / root / name
        if candidate.is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _short_from_pyproject(pyproject: dict) -> str:
    """`agentix-primitive-bash` → `bash`."""
    name = pyproject.get("project", {}).get("name", "")
    if not isinstance(name, str) or not name.startswith("agentix-"):
        raise SystemExit(
            f"pyproject.toml: name {name!r} must start with `agentix-`"
        )
    parts = name.split("-")
    if len(parts) < 3:
        raise SystemExit(
            f"pyproject.toml: name {name!r} expected `agentix-<kind>-<short>`"
        )
    return parts[-1]


def _short_from_image(ref: str) -> str:
    """`docker.io/me/agentix/primitive-bash:0.1.0` → `bash`."""
    last = ref.rsplit("/", 1)[-1].rsplit(":", 1)[0]
    for kind in CLOSURE_KINDS:
        pre = f"agentix-{kind}-"
        if last.startswith(pre):
            return last[len(pre):]
    return last


def read_pyproject(closure_dir: Path) -> dict:
    pp = closure_dir / "pyproject.toml"
    if not pp.is_file():
        raise SystemExit(f"{closure_dir}: missing pyproject.toml")
    with pp.open("rb") as f:
        return tomllib.load(f)


def resolve_spec(spec: str) -> ClosureSpec:
    if _looks_like_path(spec):
        p = Path(spec).resolve()
        if not (p / "pyproject.toml").is_file():
            raise SystemExit(f"{spec}: no pyproject.toml — not a closure source dir")
        pyproject = read_pyproject(p)
        short = _short_from_pyproject(pyproject)
        return ClosureSpec(short=short, kind="path", path=p)
    if _looks_like_image_ref(spec):
        return ClosureSpec(short=_short_from_image(spec), kind="image", image_ref=spec)
    local = _find_local(spec)
    if local is not None:
        return ClosureSpec(short=spec, kind="path", path=local)
    # PyPI fallback. We don't know which kind matches without hitting the
    # index — record the placeholder and let the caller surface a
    # NotImplementedError consistently when PyPI sourcing isn't wired.
    return ClosureSpec(
        short=spec, kind="pypi", pypi_dist=f"agentix-???-{spec}",
    )


__all__ = [
    "CLOSURE_KINDS",
    "EXTENSION_ROOTS",
    "REPO_ROOT",
    "ClosureSpec",
    "read_pyproject",
    "resolve_spec",
]
