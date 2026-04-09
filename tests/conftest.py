"""Shared fixtures for agentix tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def dummy_agent(tmp_path: Path) -> Path:
    """Create a minimal agent with async def run(ctx) -> dict."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "runner.py").write_text(textwrap.dedent("""\
        async def run(ctx: dict) -> dict:
            return {"answer": 42}
    """))
    return agent_dir


@pytest.fixture
def dummy_agent_with_schema(tmp_path: Path) -> Path:
    """Agent with CTX_SCHEMA declaring requires/optional/provides."""
    agent_dir = tmp_path / "agent_schema"
    agent_dir.mkdir()
    (agent_dir / "runner.py").write_text(textwrap.dedent("""\
        CTX_SCHEMA = {
            "requires": {
                "prompt": {"type": "str", "description": "The prompt to run"},
            },
            "optional": {
                "temperature": {"type": "float", "default": 0.7},
            },
            "provides": {
                "answer": {"type": "str", "description": "The answer"},
            },
        }

        async def run(ctx: dict) -> dict:
            return {"answer": str(ctx.get("prompt", ""))}
    """))
    return agent_dir


@pytest.fixture
def dummy_dataset(tmp_path: Path) -> Path:
    """Dataset plugin with setup and verify."""
    ds_dir = tmp_path / "dataset"
    ds_dir.mkdir()
    (ds_dir / "dataset.py").write_text(textwrap.dedent("""\
        async def setup(ctx: dict) -> dict:
            return {"prompt": "hello world"}

        async def verify(ctx: dict) -> dict:
            return {"score": 1.0}
    """))
    return ds_dir


@pytest.fixture
def dummy_agent_with_teardown(tmp_path: Path) -> Path:
    """Agent with run, teardown, and on_error hooks."""
    agent_dir = tmp_path / "agent_teardown"
    agent_dir.mkdir()
    # Use a shared list via a module-level variable to track calls
    (agent_dir / "runner.py").write_text(textwrap.dedent("""\
        call_log = []

        async def run(ctx: dict) -> dict:
            call_log.append("run")
            if ctx.get("should_fail"):
                raise RuntimeError("intentional failure")
            return {"ok": True}

        async def teardown(ctx: dict) -> None:
            call_log.append("teardown")

        async def on_error(ctx: dict, exc: Exception) -> None:
            call_log.append(f"on_error:{type(exc).__name__}")
    """))
    return agent_dir
