"""Runtime transport wire types.

Every type here is part of the HTTP / Socket.IO surface between
`RuntimeClient` (orchestrator side) and the runtime server (sandbox
side). Both client and server import from here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from agentix.runtime.shared.idents import CallId, FunctionName, ModulePath, TargetName


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class RemoteRequest(BaseModel):
    """Remote call request.

    `target` is the function address derived from the caller's function
    object: `fn.__module__ + "::" + fn.__name__`.
    """

    target: TargetName
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    call_id: CallId | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        module, sep, function = value.partition("::")
        if sep != "::" or not module or not function or "::" in function:
            raise ValueError("target must be in 'module.path::function_name' form")
        return value

    @property
    def module(self) -> ModulePath:
        module, _, _ = str(self.target).partition("::")
        return ModulePath(module)

    @property
    def function(self) -> FunctionName:
        _, _, function = str(self.target).partition("::")
        return FunctionName(function)


class RemoteError(BaseModel):
    type: str
    message: str
    traceback: str | None = None
    cancelled: bool = False


class RemoteResponse(BaseModel):
    """POST /_remote response."""

    ok: bool
    value: Any = None
    error: RemoteError | None = None
