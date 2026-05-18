"""Real importable target for the subprocess worker tests.

Lives in tests/ so the worker subprocess can import
`tests._worker_target` without a separate package install.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel


class EchoResult(BaseModel):
    msg: str


class Echo:
    @staticmethod
    async def echo(msg: str) -> EchoResult:
        return EchoResult(msg=f"echo:{msg}")

    @staticmethod
    async def counter(n: int) -> AsyncIterator[int]:
        for i in range(n):
            yield i


async def echo(msg: str) -> EchoResult:
    return await Echo.echo(msg)


async def counter(n: int) -> AsyncIterator[int]:
    async for item in Echo.counter(n):
        yield item
