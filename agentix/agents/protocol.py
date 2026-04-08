"""Agent protocol: every agent runner must implement this interface.

Each agents/{name}/runner.py must export:

    async def run(agent_input: dict) -> RunResult

RunResult contains the agent output and an ATIF trajectory
for training data collection.
"""

from __future__ import annotations

from typing import Callable, Coroutine, Any

from pydantic import BaseModel

from agentix.trajectory import Trajectory


class RunResult(BaseModel):
    """Standard return type for agent runners."""

    output: dict  # agent-specific output (exit_code, stdout, etc.)
    trajectory: Trajectory | None = None  # ATIF trajectory for training


RunFn = Callable[[dict], Coroutine[Any, Any, RunResult]]
