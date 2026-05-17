"""Read a project's pyproject.toml and derive build metadata.

`agentix build` takes one project root — a directory containing
`pyproject.toml`. Plugins (other `agentix-*` packages) are pulled in
transitively via pip from the user's declared `[project].dependencies`;
neither the CLI nor the user enumerates them on the command line.

This module owns the small bit of metadata extraction the build needs:
  * `read_pyproject(path)` — parse the project's pyproject.toml.
  * `short_name(pyproject)` — display/tag short name. Prefers the first
    `[project.entry-points."agentix.namespace"]` key (plugins) and falls
    back to the dist name (user projects).
  * `derive_tag(pyproject)` — `<short>:<version>` from name+version.

There's no multi-spec resolver, no PyPI fallback, no path-vs-image
disambiguation. The spec is always a local project root.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_pyproject(project_dir: Path) -> dict:
    pp = project_dir / "pyproject.toml"
    if not pp.is_file():
        raise SystemExit(f"{project_dir}: missing pyproject.toml")
    with pp.open("rb") as f:
        return tomllib.load(f)


def short_name(pyproject: dict) -> str:
    """Derive a short display/tag name for the project.

    Priority:
      1. First key in `[project.entry-points."agentix.namespace"]` —
         a plugin's self-declared short name.
      2. `[project].name` with an optional `agentix-` prefix stripped.

    The short name only affects the image tag and a few build
    diagnostics — wire routing is by `fn.__module__`, which is
    determined by the user's actual Python import path.
    """
    project = pyproject.get("project", {})
    ep_group = project.get("entry-points", {}).get("agentix.namespace", {})
    if isinstance(ep_group, dict) and ep_group:
        return next(iter(ep_group))
    name = project.get("name", "")
    if not isinstance(name, str) or not name:
        raise SystemExit(
            "pyproject.toml: [project].name is required when no "
            "[project.entry-points.\"agentix.namespace\"] is declared"
        )
    return name.removeprefix("agentix-")


def derive_tag(pyproject: dict) -> str:
    """`<short>:<version>` from the pyproject."""
    project = pyproject.get("project", {})
    version = project.get("version")
    if not isinstance(version, str):
        raise SystemExit("pyproject.toml: [project].version is required")
    return f"{short_name(pyproject)}:{version}"


__all__ = ["REPO_ROOT", "derive_tag", "read_pyproject", "short_name"]
