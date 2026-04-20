"""Shared fixtures for agentix tests."""

from __future__ import annotations

import os
import socket
import stat
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def tmp_socket_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the loader's socket directory and /mnt root to per-test tmp paths."""
    sock_dir = tmp_path / "sockets"
    sock_dir.mkdir()
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()
    monkeypatch.setenv("AGENTIX_SOCKET_DIR", str(sock_dir))
    monkeypatch.setenv("AGENTIX_CLOSURE_MOUNT_ROOT", str(mount_root))
    # Force loader module to re-read env.
    import importlib

    if "agentix.runtime.loader" in sys.modules:
        importlib.reload(sys.modules["agentix.runtime.loader"])
    return sock_dir


@pytest.fixture
def mount_root(tmp_socket_dir: Path) -> Path:
    """The per-test /mnt root (sibling of tmp_socket_dir)."""
    return tmp_socket_dir.parent / "mnt"


@pytest.fixture
def mount_closure(mount_root: Path):
    """Callable helper: `mount_closure(closure_path, "foo")` creates
    <mount_root>/foo/entry → closure_path, mimicking what the deployment
    layer does by mounting the closure volume at /mnt/foo.

    The `closure_path` fixture (echo_closure) already has bin/start at its
    root. We wrap it so /mnt/<ns>/entry/bin/start resolves correctly.
    """
    def _mount(closure_path: Path, namespace: str) -> Path:
        ns_dir = mount_root / namespace
        ns_dir.mkdir(exist_ok=True)
        entry = ns_dir / "entry"
        if entry.exists() or entry.is_symlink():
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                import shutil

                shutil.rmtree(entry)
        entry.symlink_to(closure_path)
        return ns_dir
    return _mount


@pytest.fixture
def echo_closure(tmp_path: Path) -> Path:
    """Build an ephemeral Python closure directory that:
    - exposes GET / with manifest
    - exposes POST /echo returning request body
    Entry point is `bin/start`, an in-tree Python script using stdlib http.server
    (no FastAPI dep needed in the test env). Reads AGENTIX_SOCKET from env —
    no CLI args — matching the Agentix closure convention.
    """
    closure = tmp_path / "echo-closure"
    (closure / "bin").mkdir(parents=True)
    script = closure / "bin" / "start"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, socket, sys, threading
            from http.server import BaseHTTPRequestHandler
            from socketserver import UnixStreamServer

            SOCKET_PATH = os.environ["AGENTIX_SOCKET"]

            MANIFEST = {
                "name": "echo",
                "version": "0.0.1",
                "kind": "tool",
                "endpoints": [
                    {"method": "GET", "path": "/", "description": "manifest"},
                    {"method": "POST", "path": "/echo"},
                ],
            }

            class H(BaseHTTPRequestHandler):
                def log_message(self, *a, **k):
                    pass
                def _json(self, code, obj):
                    body = json.dumps(obj).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def do_GET(self):
                    if self.path == "/":
                        print("got GET /", flush=True)
                        return self._json(200, MANIFEST)
                    self._json(404, {"error": "not found"})
                def do_POST(self):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    try:
                        data = json.loads(raw.decode()) if raw else None
                    except Exception:
                        data = {"_raw": raw.decode(errors="replace")}
                    print("handled POST", self.path, flush=True)
                    self._json(200, {"path": self.path, "data": data})

            class UDSHTTPServer(UnixStreamServer):
                def get_request(self):
                    request, _ = super().get_request()
                    return request, ("unix", 0)

            server = UDSHTTPServer(SOCKET_PATH, H)
            print(f"listening on {SOCKET_PATH}", flush=True)
            server.serve_forever()
            """
        )
    )
    os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return closure
