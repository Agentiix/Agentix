"""Dataset plugin protocol.

Every dataset plugin provides a dataset.py with:

    async def setup() -> dict        # init environment, return agent_input
    async def verify() -> dict       # after agent runs, collect metrics

Both run inside the sandbox with direct filesystem access.
"""

from __future__ import annotations

from typing import Callable, Coroutine, Any

from pydantic import BaseModel

from agentix.agents.protocol import RunResult


class EvalResult(BaseModel):
    """Complete result of one evaluation: setup → run → verify."""

    agent_output: dict
    trajectory: dict | None = None
    metrics: dict


SetupFn = Callable[[], Coroutine[Any, Any, dict]]
VerifyFn = Callable[[], Coroutine[Any, Any, dict]]
