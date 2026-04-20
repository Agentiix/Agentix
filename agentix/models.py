"""Shared models for Agentix.

Closures are static per sandbox: deployment mounts them at /mnt/<ns>,
runtime scans /mnt and auto-loads on startup. No HTTP /load.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Closure manifest (returned by closure's GET /) ────────────────


class Endpoint(BaseModel):
    method: str
    path: str
    description: str | None = None


class ClosureManifest(BaseModel):
    name: str
    version: str
    description: str | None = None
    kind: str | None = Field(
        default=None,
        description="Optional, purely informational (e.g. 'agent', 'dataset', 'tool'). Runtime ignores.",
    )
    endpoints: list[Endpoint] = Field(default_factory=list)

    model_config = {"extra": "allow"}


# ── Runtime server wire types ─────────────────────────────────────


class ClosureInfo(BaseModel):
    name: str
    path: str
    pid: int
    socket: str
    manifest: ClosureManifest | None = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


class LogsResponse(BaseModel):
    namespace: str
    stdout: str
    stderr: str


# ── Runtime I/O primitives (exec / upload / download / ls) ────────


class ExecRequest(BaseModel):
    command: str
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: float | None = None
    max_output: int | None = Field(
        default=None,
        description="Cap on stdout/stderr bytes for buffered exec. Default: 10 MiB.",
    )
    paths_from: list[str] | None = Field(
        default=None,
        description=(
            "Namespaces of loaded closures whose `bin/` should be prepended to PATH "
            "for this command. Default: PATH is the task image's default, closure "
            "bins do not shadow it. Use ['<ns>'] or ['*'] when you explicitly want a "
            "closure's tools on PATH."
        ),
    )


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class UploadResponse(BaseModel):
    path: str
    size: int


class LsEntry(BaseModel):
    name: str
    is_dir: bool
    size: int
    mtime: float


# ── Deployment ────────────────────────────────────────────────────


class SandboxConfig(BaseModel):
    image: str = Field(description="Base Docker/OCI image the sandbox runs on (the task environment)")
    runtime: str = Field(description="Runtime closure image ref")
    closures: dict[str, str] = Field(
        default_factory=dict,
        description="Closures to mount: {namespace: closure-image-ref}.",
    )
    env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional env vars passed to the sandbox container (and therefore "
            "visible to the runtime + all closures)."
        ),
    )


class SandboxInfo(BaseModel):
    sandbox_id: str
    runtime_url: str
    status: str = "running"
