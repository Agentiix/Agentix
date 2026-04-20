# Development Guide

## Prerequisites

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) — the only host requirement. Nix runs inside the builder stage of `templates/closure-docker/Dockerfile`.

## Setup

```bash
git clone https://github.com/Agentiix/Agentix.git
cd Agentix
pip install -e '.[dev]'        # fastapi, uvicorn, httpx, pytest, ruff
```

## Daily Workflow

### Run the runtime server locally (no sandbox)

```bash
agentix-server                 # http://localhost:8000
agentix-server --port 9000
```

Without any closures mounted, the server exposes its built-ins (`/health`, `/exec`, `/upload`, `/download`, `/ls`). Useful for iterating on the runtime without the Docker round-trip.

### Build closures

Every image — runtime and closures — builds with the same template:

```bash
docker build -t agentix/runtime:dev      -f templates/closure-docker/Dockerfile .
docker build -t agentix/mock-agent:dev   -f templates/closure-docker/Dockerfile tests/closures/mock-agent
docker build -t agentix/mock-dataset:dev -f templates/closure-docker/Dockerfile tests/closures/mock-dataset
```

### Smoke-test end-to-end in Docker

```python
import asyncio
from agentix.deployment.docker import DockerDeployment
from agentix.models import SandboxConfig
from agentix.runtime.client import RuntimeClient

async def main():
    async with DockerDeployment() as d:
        sb = await d.create(SandboxConfig(
            image="ubuntu:24.04",
            runtime="agentix/runtime:dev",
            closures={"agent": "agentix/mock-agent:dev"},
        ))
        async with RuntimeClient(sb.runtime_url) as c:
            print(await c.exec("uname -a"))
            print(await c.call("agent", "run", {"instruction": "hi"}))

asyncio.run(main())
```

`tests/smoke_docker.py` is the canonical end-to-end script — the CI `e2e` job runs it.

### Lint & test

```bash
ruff check agentix/ tests/
pytest                      # unit tests
pytest -x                   # stop on first failure
```

## Adding a new closure

Everything inside the sandbox is a closure. Copy a reference:

```bash
cp -r tests/closures/mock-agent my-closure
# edit my-closure/default.nix and the source package for your logic
docker build -t my-closure:0.1.0 -f templates/closure-docker/Dockerfile ./my-closure
```

Use it:

```python
SandboxConfig(
    image="ubuntu:24.04",
    runtime="agentix/runtime:dev",
    closures={"mine": "my-closure:0.1.0"},
)
```

See `docs/closure-protocol.md` for the closure ABI and `templates/closure-docker/README.md` for the Dockerfile contract.
