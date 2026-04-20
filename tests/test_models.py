"""Tests for agentix.models — pydantic validation and serialization."""

from __future__ import annotations

import pytest

from agentix.models import (
    ClosureManifest,
    Endpoint,
    SandboxConfig,
)


def test_closure_manifest_minimal():
    m = ClosureManifest(name="core", version="0.1.0")
    assert m.endpoints == []
    assert m.kind is None


def test_closure_manifest_full():
    m = ClosureManifest.model_validate(
        {
            "name": "mock-agent",
            "version": "0.1.0",
            "kind": "agent",
            "description": "echo",
            "endpoints": [{"method": "POST", "path": "/run"}],
            "extra_field": "ignored",  # extra=allow
        }
    )
    assert m.name == "mock-agent"
    assert m.kind == "agent"
    assert m.endpoints == [Endpoint(method="POST", path="/run")]


def test_sandbox_config_simple():
    cfg = SandboxConfig(
        image="ubuntu:24.04",
        runtime="agentix/runtime:0.1.0",
        closures={
            "claude": "agentix/claude-code:1.0.0",
            "swebench": "agentix/swebench:1.0.0",
        },
    )
    assert cfg.runtime == "agentix/runtime:0.1.0"
    assert cfg.closures["claude"] == "agentix/claude-code:1.0.0"
    assert cfg.env is None


def test_sandbox_config_with_env():
    cfg = SandboxConfig(
        image="ubuntu:24.04",
        runtime="agentix/runtime:0.1.0",
        closures={"claude": "agentix/claude-code:1.0.0"},
        env={"ANTHROPIC_API_KEY": "x"},
    )
    assert cfg.env == {"ANTHROPIC_API_KEY": "x"}


def test_sandbox_config_requires_runtime():
    with pytest.raises(Exception):
        SandboxConfig(image="ubuntu:24.04")  # type: ignore[call-arg]
