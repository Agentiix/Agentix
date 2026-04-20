<div align="center">

# Agentix

**A Nix-closure runtime for Docker sandboxes.**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

</div>

## ✨ What it is

A small framework for packaging any command as a **closure** (a Nix-built Docker image containing `/nix/store` + `/nix/entry/bin/start`), mounting multiple closures into a single sandbox, and exposing each over HTTP via a reverse-proxy.

Scope for v0.1.0 is deliberately narrow: closure packaging, sandbox composition, runtime server + reverse proxy. Higher-level abstractions (agent adapters, dataset runners, benchmark orchestration) are **out of scope for this release** — they'll be layered on top once the substrate settles.

## 📦 Build & package

Only Docker is required on the host. Nix runs inside a `nixos/nix` builder stage of the template Dockerfile at `templates/closure-docker/Dockerfile`.

```bash
git clone https://github.com/Agentiix/Agentix.git
cd Agentix
pip install -e '.[dev]'

# Every image — runtime and closures — builds with the same template:
docker build -t agentix/runtime:0.1.0      -f templates/closure-docker/Dockerfile .
docker build -t agentix/mock-agent:0.1.0   -f templates/closure-docker/Dockerfile tests/closures/mock-agent
docker build -t agentix/mock-dataset:0.1.0 -f templates/closure-docker/Dockerfile tests/closures/mock-dataset
```

### Writing your own closure

1. Drop a `default.nix` and your source files into a directory. The derivation's output must contain `bin/start` — a no-CLI-args executable that reads `AGENTIX_SOCKET` from env and binds an HTTP server on that Unix socket.
2. `docker build -t my-closure:1.0 -f <agentix>/templates/closure-docker/Dockerfile ./my-closure`

The template handles the rest: runs `nix-build`, gathers the closure of `/nix/store` deps, arranges `/nix/entry` to point at the derivation root, declares `VOLUME /nix`. See `templates/closure-docker/README.md` and `tests/closures/mock-agent/` for working references.

## 🚀 Quick start

```python
import asyncio
from agentix.deployment.docker import DockerDeployment
from agentix.models import SandboxConfig
from agentix.runtime.client import RuntimeClient

async def main():
    async with DockerDeployment() as deployment:
        sandbox = await deployment.create(SandboxConfig(
            image="ubuntu:24.04",
            runtime="agentix/runtime:0.1.0",
            closures={"echo": "agentix/mock-agent:0.1.0"},
        ))
        async with RuntimeClient(sandbox.runtime_url) as c:
            print(await c.exec("uname -a"))
            print(await c.call("echo", "run", {"instruction": "hello"}))

asyncio.run(main())
```

Under the hood the deployment:

1. For each closure image, populates a per-image named volume keyed by image digest (`docker run --rm -v vol:/nix <image> true` — Docker's own volume-init-from-image rule does the copy, idempotent).
2. Starts the sandbox with `-v agentix-closure-<digest>:/mnt/<ns>:ro` per closure + `--tmpfs /nix`.
3. The sandbox's entrypoint builds a `/nix/store` symlink forest from each mounted closure's store contents, then execs `/mnt/runtime/entry/bin/start`.
4. The runtime server's startup scans `/mnt/*` and forks each closure's `entry/bin/start`. No dynamic `/load` — sandbox contents are fixed at create time.

## 🏗️ Architecture

```
Orchestrator ──HTTP──► Runtime Server ──UDS──► Closure processes
```

| Component | Role |
|---|---|
| Runtime server | Built-ins: `/health`, `/exec`, `/upload`, `/download`, `/ls`. Introspection: `/closures`, `/closures/{ns}/logs`. Streaming reverse proxy: `ANY /{ns}/{path*}`. |
| Closure | Nix-built Docker image satisfying the closure convention (`VOLUME /nix`, `/nix/store/*`, `/nix/entry/bin/start`). |
| Deployment | Creates sandboxes, populates per-closure named volumes, bootstraps the runtime. |

See `docs/architecture.md` and `docs/closure-protocol.md` for protocol details.

## 🗺️ Roadmap

See [ROADMAP.md](ROADMAP.md).

## 🤝 Contributing

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## 📄 License

[MIT License](LICENSE)
