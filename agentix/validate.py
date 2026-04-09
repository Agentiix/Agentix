"""Validate plugins without running them (dry-run).

Usage:
    python -m agentix.validate --agent ./agents/claude-code [--dataset ./datasets/hello]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path


class PluginLoadError(Exception):
    """Raised when a plugin fails to load."""


def _load_module(path: Path, name: str):
    if not path.exists():
        raise PluginLoadError(f"Plugin file not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"Cannot import {path} — is it a valid Python file?")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginLoadError(f"Failed to load {path}: {exc}") from exc
    return module


def _validate_agent(module, path: Path):
    if not hasattr(module, "run"):
        raise PluginLoadError(f"{path} must define: async def run(ctx: dict) -> dict")
    if not asyncio.iscoroutinefunction(module.run):
        raise PluginLoadError(f"{path}: run() must be async (use 'async def run')")


def _validate_dataset(module, path: Path):
    for fn_name in ("setup", "verify"):
        if hasattr(module, fn_name) and not asyncio.iscoroutinefunction(getattr(module, fn_name)):
            raise PluginLoadError(f"{path}: {fn_name}() must be async")


def validate_plugin(path: Path, kind: str) -> list[str]:
    """Validate a plugin without running it. Returns list of issues (empty = OK)."""
    issues: list[str] = []

    # Determine entry file
    entry = "runner.py" if kind == "agent" else "dataset.py"
    entry_path = path / entry
    if not entry_path.exists():
        return [f"Missing {entry} in {path}"]

    # Try loading the module
    try:
        module = _load_module(entry_path, kind)
    except PluginLoadError as e:
        return [str(e)]

    # Check required functions via existing validators
    try:
        if kind == "agent":
            _validate_agent(module, entry_path)
        elif kind == "dataset":
            _validate_dataset(module, entry_path)
            # Datasets should define at least one of setup/verify
            if not hasattr(module, "setup") and not hasattr(module, "verify"):
                issues.append("Must define at least one of: setup(ctx), verify(ctx)")
    except PluginLoadError as e:
        issues.append(str(e))

    # Check manifest.json if present
    manifest_path = path / "manifest.json"
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text())
            if "name" not in m:
                issues.append("manifest.json missing 'name' field")
            if "kind" not in m:
                issues.append("manifest.json missing 'kind' field")
        except json.JSONDecodeError as e:
            issues.append(f"Invalid manifest.json: {e}")

    return issues


def _print_result(kind: str, path: Path, issues: list[str]) -> None:
    """Print OK or ERR line for a plugin."""
    name = path.resolve().name
    if not issues:
        print(f"OK  {kind}  {name}")
    else:
        for issue in issues:
            print(f"ERR {kind}  {name}  {issue}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate agentix plugins without running them",
    )
    parser.add_argument("--agent", type=Path, default=None,
                        help="Path to agent plugin directory")
    parser.add_argument("--dataset", type=Path, default=None,
                        help="Path to dataset plugin directory")
    args = parser.parse_args()

    if args.agent is None and args.dataset is None:
        parser.error("At least one of --agent or --dataset is required")

    has_errors = False

    if args.agent is not None:
        issues = validate_plugin(args.agent, "agent")
        _print_result("agent", args.agent, issues)
        if issues:
            has_errors = True

    if args.dataset is not None:
        issues = validate_plugin(args.dataset, "dataset")
        _print_result("dataset", args.dataset, issues)
        if issues:
            has_errors = True

    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
