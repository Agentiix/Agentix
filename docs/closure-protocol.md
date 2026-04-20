# Closure Protocol (v0.1.0)

A **closure** is a Docker image satisfying the Agentix closure convention. Inside a sandbox, the deployment mounts one closure per namespace at `/mnt/<ns>`; the runtime server forks each closure's entry point and reverse-proxies HTTP requests to it over a Unix socket.

## Image convention

A closure image MUST:

1. Declare `VOLUME /nix` (so Docker's volume-init-from-image rule populates a named volume on first attach).
2. Contain `/nix/store/<hash>-*/` — the content-addressed Nix dependencies (typically the full transitive closure of the derivation).
3. Contain `/nix/entry/bin/start` — an executable entry point.

Beyond that, the image's base layer and other contents are irrelevant — the runtime only reads what's under `/nix`.

## `start` ABI

The runtime invokes `start` with **no CLI arguments**. Contract:

- Read `AGENTIX_SOCKET` from env — the absolute path where `start` must bind a Unix-socket HTTP server.
- Bind, listen, serve. On shutdown the loader sends `SIGTERM` first, then `SIGKILL` after a short grace period — a well-behaved closure exits on `SIGTERM` to avoid dropped in-flight requests.
- MAY expose `GET /` returning a JSON manifest (see below). Optional, used by the loader for introspection (`/closures` endpoint).

Everything else — routes, request/response schemas, streaming semantics, error conventions — is the closure's choice. The runtime just proxies bytes.

## Optional manifest

```json
{
  "name": "my-closure",
  "version": "1.0.0",
  "kind": "tool",
  "description": "Short blurb",
  "endpoints": [
    {"method": "POST", "path": "/do",  "description": "Do the thing"}
  ]
}
```

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | Human-readable name |
| `version` | yes | Semantic version |
| `description` | no | Short description |
| `kind` | no | Free-form tag for tooling; runtime ignores |
| `endpoints` | no | Declared surface — informational only; `[]` if omitted |

Extra fields allowed and preserved.

## Sandbox-side placement

After the deployment puts each closure's Nix content into a per-image named volume and mounts each at `/mnt/<ns>:ro`, a sandbox sees:

```
/mnt/<ns>/
├── store/<hash>-*/         ← Nix deps (used by the symlink forest)
└── entry/
    └── bin/start           ← the entry point
```

and

```
/nix/
└── store/<hash>-*/         ← tmpfs, symlinked from /mnt/*/store/*
```

Every Nix binary's absolute `/nix/store/<hash>` reference resolves through the symlink forest.

## Runtime lifecycle

```
Sandbox boot
    │
    ├─ tmpfs /nix
    ├─ mkdir /nix/store
    ├─ ln -sfn /mnt/*/store/*  /nix/store/
    └─ exec /mnt/runtime/entry/bin/start
           │
           └─ lifespan: scan /mnt/* (skip `runtime`)
                for each /mnt/<ns>/entry/bin/start:
                    fork: exec `start` with
                          AGENTIX_SOCKET=/tmp/agentix/<ns>.sock
                          PATH=/mnt/<ns>/entry/bin:<scrubbed>
                wait for socket, optionally GET / for manifest
```

Closures are **fixed at sandbox create time**. There is no dynamic load/unload — to change the set, recreate the sandbox.

## Reverse proxy

`ANY /{namespace}/{path*}` on the runtime forwards to the closure at `/tmp/agentix/<namespace>.sock`:

- Status code, body, and non-hop-by-hop headers forwarded verbatim.
- Response streamed (`httpx.stream` → `StreamingResponse`) — SSE and chunked responses pass through.
- Hop-by-hop headers (`Host`, `Transfer-Encoding`, `Content-Length`, `Content-Encoding`) stripped on both sides.
- `502` only when the closure process is missing / dead / unreachable.

W3C `traceparent` / `tracestate` headers pass through untouched, so an OpenTelemetry-instrumented closure sees the caller's trace context automatically.

## Runtime built-ins

Independent of any closure, the runtime exposes:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /exec` | Run a shell command in the sandbox. Body `{command, cwd?, env?, timeout?, paths_from?}`. SSE when `Accept: text/event-stream`; else JSON `{exit_code, stdout, stderr}`. |
| `POST /upload` | Multipart upload into `AGENTIX_UPLOAD_ROOT` (default `/workspace`). |
| `GET /download?path=…` | Stream a file back. |
| `GET /ls?path=…` | Directory listing. |
| `GET /closures` | List loaded closures and their manifests. |
| `GET /closures/{ns}/logs` | Ring-buffered stdout/stderr of that closure's process. |

`RuntimeClient.exec / upload / download / ls / closures / logs` are typed Python helpers.

### `/exec` env and PATH

Subprocesses run with a scrubbed env:

- Stripped: `LD_LIBRARY_PATH`, `LD_PRELOAD`, `PYTHONPATH`, `PYTHONHOME`, `LOCALE_ARCHIVE`, `FONTCONFIG_*`, `SSL_CERT_FILE`, anything prefixed `NIX_`.
- Default PATH: the task image's (`/usr/local/bin:/usr/bin:/bin`). Closure-bundled tools do not shadow task-image tools.
- Opt-in to a closure's bins with `paths_from=["<ns>"]` — prepends `/mnt/<ns>/entry/bin`.

## Writing a closure

Minimal Python-closure example: a directory with

- `pyproject.toml` declaring `[project.scripts] start = "<pkg>.__main__:main"` and `fastapi` + `uvicorn` in `dependencies`
- a package with `__main__.py` that binds uvicorn on `AGENTIX_SOCKET`
- a `default.nix` that uses `buildPythonApplication` (or equivalent) to emit `bin/start`

Build the image:

```bash
docker build -t my-closure:1.0 -f /path/to/agentix/templates/closure-docker/Dockerfile ./my-closure
```

Use it:

```python
SandboxConfig(
    image="ubuntu:24.04",
    runtime="agentix/runtime:0.1.0",
    closures={"mine": "my-closure:1.0"},
)
```

See `tests/closures/mock-agent/` and `tests/closures/mock-dataset/` for working references.
