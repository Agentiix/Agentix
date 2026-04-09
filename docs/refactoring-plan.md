# Refactoring Plan

Based on architecture review (docs/architecture-review.md) and plugin system
research (docs/plugin-system-research.md). 15 issues across 3 dimensions,
organized into 3 implementation phases.

## Design Principles

1. **Plugins stay zero-dependency.** All validation/schema is opt-in and enforced
   at the framework boundary, never inside plugins.
2. **Borrow proven patterns.** Terraform schema model for ctx contracts, FastAPI
   lifespan for lifecycle, VSCode manifest for discovery, K8s reconcile for updates.
3. **No new heavy deps.** Retries are hand-rolled, logging is stdlib, tests are pytest.
4. **Backward compatible.** Every new feature (teardown, CTX_SCHEMA, manifest.json)
   is optional. Existing plugins work unchanged.

---

## Phase 1: Security + Eval Hardening

All items are small effort, independent, can be done in parallel.
This phase fixes the most critical gaps before adding features.

### R4: Runtime server authentication

**Pattern:** Bearer token middleware (standard HTTP auth).

**Files:** `agentix/runtime/server.py`, `agentix/runtime/client.py`, `agentix/deployment/docker.py`

**Design:**
```python
# server.py — FastAPI middleware
import os, secrets
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN = os.environ.get("AGENTIX_TOKEN", "")

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if TOKEN and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)
```
```python
# client.py — pass token in constructor
def __init__(self, base_url: str, token: str | None = None, timeout: float = 300):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)
```
```python
# docker.py — generate token, inject as env var
token = secrets.token_urlsafe(32)
cmd.extend(["-e", f"AGENTIX_TOKEN={token}"])
# store token in _DockerSandbox for client creation
```

**Effort:** S | **Priority:** Critical

---

### R5: Upload path traversal guard

**Pattern:** Confine writes to an allowed root directory.

**Files:** `agentix/runtime/executor.py`

**Design:**
```python
import os
UPLOAD_ROOT = Path(os.environ.get("AGENTIX_UPLOAD_ROOT", "/workspace"))

def upload(self, data: bytes, dest: str) -> int:
    p = Path(dest).resolve()
    if not p.is_relative_to(UPLOAD_ROOT):
        raise PermissionError(f"Upload path {p} outside allowed root {UPLOAD_ROOT}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return len(data)
```

**Effort:** S | **Priority:** Critical

---

### R6: Overall eval timeout

**Files:** `agentix/eval.py`

**Design:**
```python
async def run_eval(agent_dir, dataset_dir, output_path, timeout: float = 3600) -> dict:
    return await asyncio.wait_for(
        _run_eval_inner(agent_dir, dataset_dir, output_path),
        timeout=timeout,
    )

# CLI: add --timeout flag
parser.add_argument("--timeout", type=float, default=3600)
```

**Effort:** S | **Priority:** High

---

### E2: Plugin lifecycle hooks

**Pattern:** FastAPI lifespan — explicit teardown/on_error, called in try/finally.

**Files:** `agentix/eval.py`, `agentix/agents/protocol.py`, `agentix/datasets/protocol.py`

**Design:**
```python
# eval.py — wrap pipeline in try/finally
async def _run_eval_inner(agent_dir, dataset_dir, output_path) -> dict:
    runner = _load_module(...)
    dataset = _load_module(...) if dataset_dir else None
    ctx = {... }

    try:
        if dataset and hasattr(dataset, "setup"):
            setup_result = await dataset.setup(ctx)
            ctx.update(setup_result)

        run_result = await runner.run(ctx)
        ctx["run_result"] = run_result

        metrics = {}
        if dataset and hasattr(dataset, "verify"):
            metrics = await dataset.verify(ctx)
    except Exception as exc:
        # on_error hooks — optional, best-effort
        for mod in [runner, dataset]:
            if mod and hasattr(mod, "on_error"):
                try:
                    await mod.on_error(ctx, exc)
                except Exception:
                    logger.exception("on_error hook failed")
        raise
    finally:
        # teardown hooks — always run, reverse order
        for mod in [dataset, runner]:
            if mod and hasattr(mod, "teardown"):
                try:
                    await mod.teardown(ctx)
                except Exception:
                    logger.exception("teardown hook failed")

    # ... write result.json ...
```

Plugins opt in by defining functions (no import needed):
```python
# runner.py — optional hooks
async def teardown(ctx: dict) -> None:
    """Clean up resources."""

async def on_error(ctx: dict, exc: Exception) -> None:
    """Handle errors (logging, cleanup)."""
```

**Effort:** S | **Priority:** High

---

### D1: Plugin loading error messages

**Pattern:** pluggy's PluginValidationError — wrap importlib errors with actionable guidance.

**Files:** `agentix/eval.py`

**Design:**
```python
class PluginLoadError(Exception):
    """Raised when a plugin fails to load."""

def _load_module(path: Path, name: str):
    if not path.exists():
        raise PluginLoadError(f"Plugin file not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"Cannot import {path} — is it a valid Python file?")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PluginLoadError(f"Failed to load {path}: {exc}") from exc
    return module

def _validate_agent(module, path: Path):
    if not hasattr(module, "run"):
        raise PluginLoadError(f"{path} must define: async def run(ctx: dict) -> dict")
    if not asyncio.iscoroutinefunction(module.run):
        raise PluginLoadError(f"{path}: run() must be async (use 'async def run')")

def _validate_dataset(module, path: Path):
    for fn_name in ("setup", "verify"):
        if hasattr(module, fn_name) and not asyncio.iscoroutinefunction(getattr(module, fn_name)):
            raise PluginLoadError(f"{path}: {fn_name}() must be async")
```

**Effort:** S | **Priority:** High

---

## Phase 2: Schema + Discovery

The core extensibility win. Builds on Phase 1 (uses improved plugin loading).

### E1: ctx schema contracts

**Pattern:** Terraform's Required/Optional/Computed model as a plain dict in the plugin.
Framework validates at the eval boundary. Plugin never imports agentix.

**Files:** new `agentix/ctx.py`, `agentix/eval.py`

**Design — plugin side (zero-dep):**
```python
# agents/claude-code/runner.py — optional, plain dict
CTX_SCHEMA = {
    "requires": {
        "instruction": {"type": "str", "description": "Task instruction"},
        "api_key": {"type": "str", "description": "Anthropic API key"},
    },
    "optional": {
        "model": {"type": "str", "default": "claude-sonnet-4-20250514"},
        "max_turns": {"type": "int"},
        "timeout": {"type": "float"},
    },
    "provides": {
        "exit_code": {"type": "int"},
        "stdout": {"type": "str"},
        "stderr": {"type": "str"},
    },
}
```

**Design — framework side (`agentix/ctx.py`):**
```python
TYPE_MAP = {"str": str, "int": int, "float": (int, float), "bool": bool, "dict": dict, "list": list}

def extract_schema(module) -> dict | None:
    """Read CTX_SCHEMA from plugin module. Returns None if not defined."""
    return getattr(module, "CTX_SCHEMA", None)

def validate_requires(ctx: dict, schema: dict, plugin_name: str) -> None:
    """Check all required keys are present and correctly typed."""
    for key, spec in schema.get("requires", {}).items():
        if key not in ctx:
            raise ValueError(f"Plugin '{plugin_name}' requires ctx['{key}'] but it's missing. "
                             f"Description: {spec.get('description', 'N/A')}")
        expected = TYPE_MAP.get(spec.get("type"))
        if expected and not isinstance(ctx[key], expected):
            raise TypeError(f"ctx['{key}'] must be {spec['type']}, got {type(ctx[key]).__name__}")

def apply_defaults(ctx: dict, schema: dict) -> dict:
    """Fill optional keys with defaults if not present."""
    for key, spec in schema.get("optional", {}).items():
        if key not in ctx and "default" in spec:
            ctx[key] = spec["default"]
    return ctx

def validate_provides(result: dict, schema: dict, plugin_name: str) -> None:
    """Warn if plugin didn't provide declared output keys."""
    for key in schema.get("provides", {}):
        if key not in result:
            logger.warning("Plugin '%s' declared provides['%s'] but didn't return it", plugin_name, key)
```

**Integration in `eval.py`:**
```python
# Before runner.run()
runner_schema = extract_schema(runner)
if runner_schema:
    validate_requires(ctx, runner_schema, "agent")
    apply_defaults(ctx, runner_schema)

# After runner.run()
if runner_schema:
    validate_provides(run_result, runner_schema, "agent")
```

**Key property:** Plugins without CTX_SCHEMA work exactly as before. Schema is
documentation-that-validates, not a forced contract.

**Effort:** M | **Priority:** High

---

### E4: Plugin discovery and manifest

**Pattern:** VSCode's `package.json` manifest + pytest's directory scanning.

**Files:** new `agentix/registry.py`, optional `manifest.json` per plugin

**Design — manifest (static, no code execution needed):**
```json
{
    "name": "claude-code",
    "version": "0.1.0",
    "kind": "agent",
    "description": "Claude Code coding agent",
    "entry": "runner.py"
}
```

For dataset plugins:
```json
{
    "name": "hello-dataset",
    "version": "0.1.0",
    "kind": "dataset",
    "description": "Example dataset for testing",
    "entry": "dataset.py"
}
```

**Design — registry (`agentix/registry.py`):**
```python
from dataclasses import dataclass
from pathlib import Path
import json

@dataclass
class PluginInfo:
    name: str
    kind: str              # "agent" | "dataset"
    path: Path             # directory containing the plugin
    entry: str             # filename: "runner.py" or "dataset.py"
    version: str | None = None
    description: str | None = None

def discover(search_dirs: list[Path]) -> list[PluginInfo]:
    """Scan directories for plugins. Reads manifest.json if present,
    falls back to detecting runner.py / dataset.py."""
    plugins = []
    for d in search_dirs:
        for child in sorted(d.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if manifest_path.exists():
                m = json.loads(manifest_path.read_text())
                plugins.append(PluginInfo(
                    name=m["name"], kind=m["kind"], path=child,
                    entry=m.get("entry", "runner.py"),
                    version=m.get("version"), description=m.get("description"),
                ))
            elif (child / "runner.py").exists():
                plugins.append(PluginInfo(
                    name=child.name, kind="agent", path=child, entry="runner.py"))
            elif (child / "dataset.py").exists():
                plugins.append(PluginInfo(
                    name=child.name, kind="dataset", path=child, entry="dataset.py"))
    return plugins

def find(name: str, kind: str, search_dirs: list[Path]) -> PluginInfo:
    """Find a specific plugin by name and kind."""
    for p in discover(search_dirs):
        if p.name == name and p.kind == kind:
            return p
    raise KeyError(f"{kind} plugin '{name}' not found in {search_dirs}")
```

**CLI:**
```bash
$ python -m agentix.registry list --search-dir ./agents --search-dir ./datasets
agent    claude-code   0.1.0  Claude Code coding agent
dataset  hello-dataset 0.1.0  Example dataset for testing
```

**Effort:** S | **Priority:** Medium

---

### D3: Validation CLI (dry-run)

**Depends on:** D1 (error messages), E1 (ctx schema)

**Files:** new `agentix/validate.py`

**Design:**
```python
"""Validate plugins without running them.

Usage:
    python -m agentix.validate --agent ./agents/claude-code [--dataset ./datasets/hello]
"""

def validate_plugin(path: Path, kind: str) -> list[str]:
    """Returns list of issues. Empty = OK."""
    issues = []
    entry = "runner.py" if kind == "agent" else "dataset.py"
    entry_path = path / entry
    if not entry_path.exists():
        return [f"Missing {entry} in {path}"]

    try:
        module = _load_module(entry_path, kind)
    except PluginLoadError as e:
        return [str(e)]

    # Check required functions
    if kind == "agent":
        if not hasattr(module, "run"):
            issues.append("Must define: async def run(ctx: dict) -> dict")
        elif not asyncio.iscoroutinefunction(module.run):
            issues.append("run() must be async")
    elif kind == "dataset":
        if not hasattr(module, "setup") and not hasattr(module, "verify"):
            issues.append("Must define at least one of: setup(ctx), verify(ctx)")

    # Check schema if present
    schema = getattr(module, "CTX_SCHEMA", None)
    if schema:
        # validate schema structure itself
        for section in ("requires", "optional", "provides"):
            if section in schema and not isinstance(schema[section], dict):
                issues.append(f"CTX_SCHEMA['{section}'] must be a dict")

    # Check manifest if present
    manifest = path / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text())
            if "name" not in m:
                issues.append("manifest.json missing 'name' field")
            if "kind" not in m:
                issues.append("manifest.json missing 'kind' field")
        except json.JSONDecodeError as e:
            issues.append(f"Invalid manifest.json: {e}")

    return issues
```

**CLI output:**
```bash
$ python -m agentix.validate --agent ./agents/claude-code
OK  agent  claude-code  (schema: 2 requires, 3 optional, 3 provides)

$ python -m agentix.validate --agent ./agents/broken
ERR agent  broken  runner.py must define: async def run(ctx: dict) -> dict
```

**Effort:** S | **Priority:** Medium

---

### D2: Example dataset plugin

**Files:** new `examples/hello-dataset/dataset.py`, new `examples/hello-dataset/manifest.json`

**Design:**
```python
# examples/hello-dataset/dataset.py
"""Example dataset: write a file, ask agent to read it, verify output."""

from pathlib import Path

CTX_SCHEMA = {
    "requires": {},
    "optional": {
        "api_key": {"type": "str", "default": "test-key"},
    },
    "provides": {
        "instruction": {"type": "str"},
    },
}

async def setup(ctx: dict) -> dict:
    task_file = Path(ctx["workdir"]) / "task.txt"
    task_file.write_text("Say hello world")
    return {
        "instruction": "Read the file task.txt and print its contents exactly.",
        "api_key": ctx.get("api_key", "test-key"),
    }

async def verify(ctx: dict) -> dict:
    output = ctx.get("run_result", {}).get("stdout", "")
    passed = "hello world" in output.lower()
    return {
        "pass": passed,
        "output_length": len(output),
        "reason": "Output contains 'hello world'" if passed else "Expected 'hello world' in output",
    }
```

```json
// examples/hello-dataset/manifest.json
{
    "name": "hello-dataset",
    "version": "0.1.0",
    "kind": "dataset",
    "description": "Trivial dataset for testing the eval pipeline",
    "entry": "dataset.py"
}
```

**Effort:** S | **Priority:** High

---

## Phase 3: Reliability + DX Polish

All independent, can be done in any order after Phase 1.

### R1: RuntimeClient retries

**Pattern:** K8s requeue-with-backoff. Hand-rolled, no tenacity dependency.

**Files:** `agentix/runtime/client.py`

**Design:**
```python
def __init__(self, base_url: str, token: str | None = None,
             timeout: float = 300, retries: int = 3, retry_backoff: float = 1.0):
    self._retries = retries
    self._retry_backoff = retry_backoff
    # ... existing init ...

async def _with_retry(self, fn, *args, **kwargs):
    """Retry on transient errors with exponential backoff."""
    last_exc = None
    for attempt in range(self._retries):
        try:
            return await fn(*args, **kwargs)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < self._retries - 1:
                wait = self._retry_backoff * (2 ** attempt)
                logger.warning("Retry %d/%d after %.1fs: %s", attempt + 1, self._retries, wait, exc)
                await asyncio.sleep(wait)
    raise last_exc
```

**Effort:** S | **Priority:** High

---

### R2: Port allocation race fix

**Files:** `agentix/deployment/docker.py`

**Design:**
```python
def __init__(self, host_port_start: int = 18000):
    self._next_port = host_port_start
    self._port_lock = asyncio.Lock()

async def _allocate_port(self) -> int:
    """Allocate an available port, thread-safe."""
    import socket
    async with self._port_lock:
        while True:
            port = self._next_port
            self._next_port += 1
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
```

**Effort:** S | **Priority:** High

---

### R3: Output size cap

**Files:** `agentix/runtime/executor.py`, `agentix/models.py`

**Design:**
```python
# executor.py
MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MiB

async def exec(self, command, timeout=None, cwd=None,
               extra_env=None, max_output: int = MAX_OUTPUT_BYTES):
    # ... create subprocess ...
    stdout = await self._read_capped(proc.stdout, max_output)
    stderr = await self._read_capped(proc.stderr, max_output)
    await proc.wait()
    return (proc.returncode or 0, stdout, stderr)

async def _read_capped(self, stream, limit: int) -> str:
    """Read from stream up to limit bytes."""
    chunks = []
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = limit - total
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            chunks.append(b"\n[truncated at %d bytes]" % limit)
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks).decode(errors="replace")

# models.py — add field
class ExecRequest(BaseModel):
    command: str
    timeout: float | None = Field(default=None)
    cwd: str | None = Field(default=None)
    env: dict[str, str] | None = Field(default=None)
    max_output: int = Field(default=10 * 1024 * 1024, description="Max output bytes")
```

**Effort:** S | **Priority:** High

---

### D4: Structured logging with run ID and timing

**Files:** `agentix/eval.py`

**Design:**
```python
import time
import uuid

async def _run_eval_inner(agent_dir, dataset_dir, output_path) -> dict:
    run_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    logger.info("[%s] eval start agent=%s dataset=%s", run_id, agent_dir, dataset_dir)

    # ... load plugins ...
    logger.info("[%s] plugins loaded in %.1fs", run_id, time.monotonic() - t0)

    # setup phase
    t_phase = time.monotonic()
    if dataset and hasattr(dataset, "setup"):
        setup_result = await dataset.setup(ctx)
        ctx.update(setup_result)
        logger.info("[%s] dataset.setup() done in %.1fs", run_id, time.monotonic() - t_phase)

    # run phase
    t_phase = time.monotonic()
    run_result = await runner.run(ctx)
    logger.info("[%s] runner.run() done in %.1fs", run_id, time.monotonic() - t_phase)

    # verify phase
    t_phase = time.monotonic()
    if dataset and hasattr(dataset, "verify"):
        metrics = await dataset.verify(ctx)
        logger.info("[%s] dataset.verify() done in %.1fs", run_id, time.monotonic() - t_phase)

    logger.info("[%s] eval complete total=%.1fs", run_id, time.monotonic() - t0)
```

**Effort:** S | **Priority:** Medium

---

### D5: Test suite

**Files:** new `tests/conftest.py`, `tests/test_eval.py`, `tests/test_executor.py`,
`tests/test_models.py`, `tests/test_ctx.py`

**Design — key fixtures:**
```python
# tests/conftest.py
import pytest
from pathlib import Path

@pytest.fixture
def dummy_agent(tmp_path):
    """Minimal agent plugin."""
    (tmp_path / "runner.py").write_text(
        'async def run(ctx: dict) -> dict:\n    return {"result": "ok"}\n'
    )
    return tmp_path

@pytest.fixture
def dummy_dataset(tmp_path):
    """Minimal dataset plugin."""
    d = tmp_path / "ds"
    d.mkdir()
    (d / "dataset.py").write_text(
        'async def setup(ctx):\n    return {"instruction": "test"}\n'
        'async def verify(ctx):\n    return {"pass": True}\n'
    )
    return d
```

**Test plan:**
```
tests/
├── conftest.py          # shared fixtures (dummy plugins, tmp dirs)
├── test_eval.py         # run_eval with mock agent/dataset, error paths
├── test_executor.py     # exec, upload, download, path traversal guard
├── test_models.py       # pydantic model round-trip, validation
├── test_ctx.py          # schema validation, defaults, provides checking
├── test_registry.py     # discover, find, manifest parsing
└── test_validate.py     # validation CLI, error detection
```

**Effort:** M | **Priority:** High

---

### E3: In-place sandbox update (reconcile)

**Pattern:** K8s reconcile — diff desired vs actual config, only recreate when necessary.

**Files:** `agentix/deployment/docker.py`, `agentix/deployment/base.py`

**Design:**
```python
# base.py — document the contract
@abstractmethod
async def update(self, sandbox_id: str, config: SandboxConfig,
                 *, force_recreate: bool = False) -> SandboxInfo:
    """Update sandbox config. Attempts in-place update when possible.
    Falls back to recreate when base image changes or force_recreate=True."""

# docker.py — reconcile logic
async def update(self, sandbox_id: str, config: SandboxConfig,
                 *, force_recreate: bool = False) -> SandboxInfo:
    sb = self._sandboxes.get(sandbox_id)
    if not sb:
        raise KeyError(f"Sandbox not found: {sandbox_id}")

    # Diff: what changed?
    image_changed = config.task_image != sb.config.task_image
    agent_changed = config.agent_closure != sb.config.agent_closure
    runtime_changed = config.runtime_closure != sb.config.runtime_closure

    if force_recreate or image_changed or runtime_changed:
        # Full recreate — can't update base image or runtime in-place
        await self.delete(sandbox_id)
        return await self.create(config)

    if agent_changed:
        # In-place: update PATH to point to new agent closure, restart server
        new_path = f"{config.agent_closure}/bin:{config.runtime_closure}/bin:/usr/local/bin:/usr/bin:/bin"
        await self._exec_in_container(sandbox_id, f"export PATH={new_path}")
        # Restart agentix-server to pick up new PATH
        await self._exec_in_container(sandbox_id, "pkill -f agentix-server || true")
        await self._exec_in_container(
            sandbox_id,
            f"PATH={new_path} {config.runtime_closure}/bin/agentix-server --port 8000 &",
        )
        sb.config = config
        return await self.get(sandbox_id)

    # Nothing changed
    return await self.get(sandbox_id)

async def _exec_in_container(self, sandbox_id: str, command: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", sandbox_id, "sh", "-c", command,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
```

**Effort:** M | **Priority:** Low

---

## Implementation Order

```
Phase 1 (parallel, ~1 day):
  R4 (auth) ──────────────┐
  R5 (path guard) ────────┤
  R6 (eval timeout) ──────┤── all touch different files
  E2 (lifecycle hooks) ────┤
  D1 (error messages) ─────┘

Phase 2 (sequential, ~2 days):
  E1 (ctx schema) ──► E4 (registry) ──► D3 (validate CLI)
  D2 (example dataset) ── independent, do alongside E1

Phase 3 (parallel, ~2 days):
  R1 (retries) ────────────┐
  R2 (port fix) ───────────┤
  R3 (output cap) ─────────┤── all independent
  D4 (logging) ────────────┤
  D5 (tests) ─────────────┤
  E3 (reconcile update) ───┘
```

## File Change Summary

**New files:**
- `agentix/ctx.py` — schema validation
- `agentix/registry.py` — plugin discovery
- `agentix/validate.py` — dry-run CLI
- `examples/hello-dataset/dataset.py` — example dataset
- `examples/hello-dataset/manifest.json` — example manifest
- `agents/claude-code/manifest.json` — manifest for existing agent
- `tests/conftest.py`, `tests/test_*.py` — test suite

**Modified files:**
- `agentix/eval.py` — lifecycle hooks, timeout, logging, schema integration
- `agentix/runtime/server.py` — auth middleware
- `agentix/runtime/client.py` — auth header, retries
- `agentix/runtime/executor.py` — path guard, output cap
- `agentix/deployment/docker.py` — token injection, port fix, reconcile update
- `agentix/deployment/base.py` — update() signature
- `agentix/models.py` — max_output field
