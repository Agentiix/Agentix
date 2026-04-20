"""End-to-end smoke test: create a real Docker sandbox with two mock closures,
verify the layout and the reverse-proxy path.

Run locally with the dev images:
    docker build -t agentix/runtime:dev      -f templates/closure-docker/Dockerfile .
    docker build -t agentix/mock-agent:dev   -f templates/closure-docker/Dockerfile tests/closures/mock-agent
    docker build -t agentix/mock-dataset:dev -f templates/closure-docker/Dockerfile tests/closures/mock-dataset
    python tests/smoke_docker.py

Override image refs via env:
    AGENTIX_RUNTIME_IMAGE, AGENTIX_AGENT_IMAGE, AGENTIX_DATASET_IMAGE
"""

from __future__ import annotations

import asyncio
import os
import sys

from agentix.deployment.docker import DockerDeployment
from agentix.models import SandboxConfig
from agentix.runtime.client import RuntimeClient

RUNTIME_IMAGE = os.environ.get("AGENTIX_RUNTIME_IMAGE", "agentix/runtime:dev")
AGENT_IMAGE = os.environ.get("AGENTIX_AGENT_IMAGE", "agentix/mock-agent:dev")
DATASET_IMAGE = os.environ.get("AGENTIX_DATASET_IMAGE", "agentix/mock-dataset:dev")


async def main() -> None:
    async with DockerDeployment(host_port_start=29000) as d:
        sb = await d.create(
            SandboxConfig(
                image="ubuntu:24.04",
                runtime=RUNTIME_IMAGE,
                closures={"agent": AGENT_IMAGE, "dataset": DATASET_IMAGE},
            )
        )
        async with RuntimeClient(sb.runtime_url) as c:
            # /mnt should have runtime + both closures
            r = await c.exec("ls /mnt")
            mnt = set(r.stdout.split())
            assert mnt == {"runtime", "agent", "dataset"}, f"unexpected /mnt: {mnt}"

            # /nix/store is the merged symlink forest
            r = await c.exec("ls /nix/store | wc -l")
            assert int(r.stdout.strip()) > 0, "no /nix/store entries merged"

            # Both closures auto-loaded by runtime lifespan
            closures = {x.name for x in await c.closures()}
            assert closures == {"agent", "dataset"}, f"wrong loaded closures: {closures}"

            # Reverse proxy to each closure
            agent_out = await c.call("agent", "run", {"instruction": "smoke", "workdir": "/tmp"})
            assert agent_out["exit_code"] == 0 and "smoke" in agent_out["patch"]

            ds_out = await c.call("dataset", "setup", {"instance_id": "smoke-001"})
            assert ds_out["instance_id"] == "smoke-001"

    print("OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
