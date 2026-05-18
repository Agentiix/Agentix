"""Branded string identifiers used by the runtime wire layer."""

from __future__ import annotations

from typing import NewType

CallId = NewType("CallId", str)
"""RPC call correlation key carried on `RemoteRequest.call_id` and
Socket.IO stream/bidi frames."""

TargetName = NewType("TargetName", str)
"""A remote function address in `module.path::function_name` form."""

ModulePath = NewType("ModulePath", str)
"""A Python import path extracted from a remote target."""

FunctionName = NewType("FunctionName", str)
"""A function name extracted from a remote target."""

__all__ = ["CallId", "FunctionName", "ModulePath", "TargetName"]
