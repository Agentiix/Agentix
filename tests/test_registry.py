"""Tests for agentix.registry — plugin discovery and manifest parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentix.registry import PluginInfo, discover, find


def test_discover_with_manifest(tmp_path):
    """Finds plugin via manifest.json."""
    plugin_dir = tmp_path / "plugins" / "my-agent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "runner.py").write_text("async def run(ctx): return {}\n")
    (plugin_dir / "manifest.json").write_text(json.dumps({
        "name": "my-agent",
        "kind": "agent",
        "version": "1.0.0",
        "description": "A test agent",
    }))
    plugins = discover([tmp_path / "plugins"])
    assert len(plugins) == 1
    p = plugins[0]
    assert p.name == "my-agent"
    assert p.kind == "agent"
    assert p.version == "1.0.0"
    assert p.description == "A test agent"


def test_discover_without_manifest(tmp_path):
    """Falls back to detecting runner.py when no manifest.json."""
    plugin_dir = tmp_path / "plugins" / "simple-agent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "runner.py").write_text("async def run(ctx): return {}\n")
    plugins = discover([tmp_path / "plugins"])
    assert len(plugins) == 1
    p = plugins[0]
    assert p.name == "simple-agent"
    assert p.kind == "agent"
    assert p.entry == "runner.py"
    assert p.version is None


def test_discover_dataset(tmp_path):
    """Finds dataset.py plugin."""
    ds_dir = tmp_path / "plugins" / "my-dataset"
    ds_dir.mkdir(parents=True)
    (ds_dir / "dataset.py").write_text("async def setup(ctx): return {}\n")
    plugins = discover([tmp_path / "plugins"])
    assert len(plugins) == 1
    assert plugins[0].kind == "dataset"
    assert plugins[0].entry == "dataset.py"


def test_find_by_name(tmp_path):
    """Find specific plugin by name and kind."""
    plugin_dir = tmp_path / "plugins" / "target-agent"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "runner.py").write_text("async def run(ctx): return {}\n")
    # Add another plugin so we're actually filtering
    other_dir = tmp_path / "plugins" / "other-agent"
    other_dir.mkdir(parents=True)
    (other_dir / "runner.py").write_text("async def run(ctx): return {}\n")

    result = find("target-agent", "agent", [tmp_path / "plugins"])
    assert result.name == "target-agent"


def test_find_not_found(tmp_path):
    """Raises KeyError when plugin not found."""
    plugin_dir = tmp_path / "plugins" / "exists"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "runner.py").write_text("async def run(ctx): return {}\n")
    with pytest.raises(KeyError, match="ghost"):
        find("ghost", "agent", [tmp_path / "plugins"])
