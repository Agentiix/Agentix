"""Stands in for a user's own importable module."""

from __future__ import annotations


async def greet(name: str) -> str:
    return f"hello {name}"


async def add(a: int, b: int) -> int:
    return a + b
