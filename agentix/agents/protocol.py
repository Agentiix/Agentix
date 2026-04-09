"""Agent plugin protocol.

runner.py must export:

    async def run(ctx: dict) -> dict

That's it. No imports required. ctx and return value are free-form dicts.
Agentix helpers (RunResult, Trajectory) are optional conveniences.
"""

from __future__ import annotations

from typing import Callable, Coroutine, Any

RunFn = Callable[[dict], Coroutine[Any, Any, dict]]
