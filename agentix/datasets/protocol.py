"""Dataset plugin protocol.

dataset.py must export:

    async def setup(ctx: dict) -> dict    # init environment, return agent input
    async def verify(ctx: dict) -> dict   # after agent runs, return metrics

Both receive ctx (free-form dict) and return free-form dict.
No imports required.
"""

from __future__ import annotations

from typing import Callable, Coroutine, Any

SetupFn = Callable[[dict], Coroutine[Any, Any, dict]]
VerifyFn = Callable[[dict], Coroutine[Any, Any, dict]]
