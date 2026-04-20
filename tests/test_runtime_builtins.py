"""Tests for the runtime server's built-in endpoints: exec/upload/download/ls.

These endpoints live on the runtime directly (no closure load required),
so we drive them through an ASGI transport on the real FastAPI app.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def builtins_app(tmp_path: Path, tmp_socket_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Point AGENTIX_UPLOAD_ROOT at a tmp dir and reload the runtime modules."""
    upload_root = tmp_path / "workspace"
    upload_root.mkdir()
    monkeypatch.setenv("AGENTIX_UPLOAD_ROOT", str(upload_root))

    import importlib
    import sys

    for mod in ("agentix.runtime.builtins", "agentix.runtime.loader", "agentix.runtime.server"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])

    from agentix.runtime import server

    return server, upload_root


async def test_exec_buffered(builtins_app):
    server, _root = builtins_app
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post("/exec", json={"command": "echo hi && echo err 1>&2"})
        assert r.status_code == 200
        body = r.json()
        assert body["exit_code"] == 0
        assert body["stdout"] == "hi\n"
        assert body["stderr"] == "err\n"


async def test_exec_respects_cwd_and_env(builtins_app):
    server, root = builtins_app
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post(
            "/exec",
            json={"command": "echo $FOO && pwd", "cwd": str(root), "env": {"FOO": "bar"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert "bar" in body["stdout"]
        assert str(root) in body["stdout"]


async def test_exec_validates_missing_command(builtins_app):
    server, _root = builtins_app
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post("/exec", json={})
        assert r.status_code in (400, 422)


async def test_exec_scrubs_nix_env(builtins_app, monkeypatch: pytest.MonkeyPatch):
    """Nix-flavoured env vars in the runtime process must not leak into subprocesses."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nix/store/xxx/lib")
    monkeypatch.setenv("NIX_CFLAGS_COMPILE", "-I/nix/store/xxx/include")
    server, _root = builtins_app
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post(
            "/exec",
            json={"command": "echo LD=$LD_LIBRARY_PATH NIX=$NIX_CFLAGS_COMPILE"},
        )
        body = r.json()
        assert body["exit_code"] == 0
        assert "LD=\n" in body["stdout"] or "LD= " in body["stdout"]
        assert "NIX=\n" in body["stdout"] or "NIX= " in body["stdout"]


async def test_upload_download_ls_round_trip(builtins_app):
    server, root = builtins_app
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        target = root / "sub" / "hello.txt"
        r = await http.post(
            "/upload",
            files={"file": ("hello.txt", b"hello world")},
            data={"path": str(target)},
        )
        assert r.status_code == 200
        assert r.json() == {"path": str(target), "size": 11}
        assert target.read_bytes() == b"hello world"

        r = await http.get("/ls", params={"path": str(root / "sub")})
        assert r.status_code == 200
        names = [e["name"] for e in r.json()]
        assert names == ["hello.txt"]

        r = await http.get("/download", params={"path": str(target)})
        assert r.status_code == 200
        assert r.content == b"hello world"


async def test_exec_paths_from_prepends_closure_bin(builtins_app, echo_closure: Path, mount_closure):
    """`paths_from=[ns]` prepends /mnt/<ns>/entry/bin to PATH."""
    server, _root = builtins_app

    closure_bin = echo_closure / "bin"
    marker = closure_bin / "my-marker"
    marker.write_text("#!/bin/sh\necho closure-wins\n")
    marker.chmod(0o755)

    mount_closure(echo_closure, "echo")
    loader = server.loader
    await loader.load("echo")
    try:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            r = await http.post(
                "/exec", json={"command": "my-marker", "paths_from": ["echo"]}
            )
            assert r.status_code == 200
            assert r.json()["stdout"].strip() == "closure-wins"

            r = await http.post("/exec", json={"command": "my-marker"})
            assert r.json()["exit_code"] != 0
    finally:
        await loader.unload("echo")


async def test_upload_rejects_path_outside_root(builtins_app):
    server, _root = builtins_app
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        r = await http.post(
            "/upload",
            files={"file": ("x.txt", b"x")},
            data={"path": "/etc/passwd"},
        )
        assert r.status_code == 403
