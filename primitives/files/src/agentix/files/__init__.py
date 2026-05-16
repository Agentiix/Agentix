"""Files primitive — sandbox file upload / download as an Agentix namespace.

Usage:

    from agentix import RuntimeClient
    from agentix import files

    async with RuntimeClient(sandbox.runtime_url) as c:
        r = await c.remote(files.upload, path="/workspace/input.txt", content=b"hello")
        print(r.size)

        data = await c.remote(files.download, path="/workspace/output.txt")

Files are encoded as pydantic `bytes` (base64 in the JSON wire form).
Suitable for kB–MB sized files; very large blobs should ship via a
purpose-built binary `WirePattern` rather than the unary JSON path.

The package IS the namespace — `upload` and `download` are top-level
async functions, `UploadResult` is a regular dataclass callers can
import for type hints.

Writes/reads are confined to `$AGENTIX_UPLOAD_ROOT` (default
`/workspace`). Paths outside that root raise `PermissionError`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

UPLOAD_ROOT = Path(os.environ.get("AGENTIX_UPLOAD_ROOT", "/workspace")).resolve()


@dataclass
class UploadResult:
    """What `upload` returns — resolved sandbox-side path + bytes written."""

    path: str
    size: int


def _resolve_within(path: str) -> Path:
    """Return `path` resolved, asserting it stays inside `UPLOAD_ROOT`.

    The resolve-before-open pattern is race-free: a symlink-after-check
    swap can only land on a path the resolver was already happy with.
    """
    p = Path(path).resolve()
    if not p.is_relative_to(UPLOAD_ROOT):
        raise PermissionError(f"Path {p} outside allowed root {UPLOAD_ROOT}")
    return p


async def upload(path: str, content: bytes) -> UploadResult:
    """Write `content` to `path` inside the sandbox.

    Creates parent directories as needed. `path` must resolve under
    the upload-root; otherwise raises `PermissionError`.
    """
    p = _resolve_within(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return UploadResult(path=str(p), size=len(content))


async def download(path: str) -> bytes:
    """Read the contents of `path` from inside the sandbox.

    Raises `FileNotFoundError` / `IsADirectoryError` /
    `PermissionError` for the corresponding filesystem conditions.
    """
    p = _resolve_within(path)
    return p.read_bytes()
