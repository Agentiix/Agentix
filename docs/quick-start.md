---
title: Quick start
description: Install Agentix, call a namespace inside a sandbox, then write your own — in five minutes.
---

# Quick start

Install Agentix, call a namespace inside a sandbox, then write your own — in five minutes.

!!! tip
    Looking for a deeper tour? See the [Plugin authors guide](plugins.md) for
    every extension axis, or [Architecture](architecture.md) for how the
    runtime dispatches calls.

## Prerequisites

- Python 3.11 or newer
- Docker (for the `local` deployment used below)

## Install

=== "pip"

    ```bash
    pip install agentix agentix-bash agentix-files
    ```

=== "uv"

    ```bash
    uv add agentix agentix-bash agentix-files
    ```

=== "From source"

    ```bash
    git clone https://github.com/Agentiix/Agentix
    cd Agentix && pip install -e '.[dev]'
    pip install -e ./primitives/bash -e ./primitives/files
    ```

`agentix` is the framework; `agentix-bash` and `agentix-files` are
**namespaces** — independently versioned wheels that contribute the
remote-callable `Bash` and `Files` classes. Every namespace, deployment, or
trace sink follows the same pattern: one wheel, one entry-point block.

## Verify the install

```bash
agentix plugins
```

```text
agentix.namespace
  bash    → agentix.bash:Bash    [agentix-bash@0.1.0]   ok
  files   → agentix.files:Files  [agentix-files@0.1.0]  ok

agentix.deployment
  local   → agentix.deployment.docker:DockerDeployment  [agentix@0.1.0] ok
  daytona → …
  e2b     → …
```

`agentix plugins` lists every installed extension across all six axes —
namespaces, deployments, trace sinks, spec resolvers, wire patterns, and
CLI subcommands. Add `--verbose` to see load tracebacks for anything that
failed to import.

## Call a namespace

```python title="hello_sandbox.py"
import asyncio
from agentix import RuntimeClient, SandboxConfig
from agentix.deployment.base import session
from agentix.deployment.docker import DockerDeployment
from agentix.bash import Bash  # (1)!

async def main():
    deployment = DockerDeployment()  # (2)!
    config = SandboxConfig(
        image="ubuntu:24.04",
        runtime="agentix/runtime:latest",
        closures=["agentix/bash:0.1.0"],  # (3)!
    )
    async with session(deployment, config) as sandbox:  # (4)!
        async with RuntimeClient(sandbox.runtime_url) as c:
            result = await c.remote(Bash.run, command="echo hi")  # (5)!
            print(result.stdout)  # → "hi\n"

asyncio.run(main())
```

1. The typed surface — `pip install agentix-bash` makes this import resolve.
   Methods carry the real implementation; you call them via `c.remote(...)`,
   which executes them inside the sandbox.
2. `DockerDeployment` is the built-in `local` deployment. Swap in another
   backend with `load_deployment("daytona")` or `load_deployment("e2b")`.
3. One closure image per namespace, pre-built by `agentix build`.
4. `session(...)` is a free function — composition over inheritance. It
   creates the sandbox on entry and tears it down on exit.
5. `c.remote` reads `Bash.run.__module__` (= `"agentix.bash"`) as the
   routing key. Unary calls go over `POST /_remote`; methods returning
   `AsyncIterator[T]` auto-upgrade to Socket.IO.

Run it:

```bash
python hello_sandbox.py
# → hi
```

The first run pulls the runtime and namespace images (≈ 30 s). Subsequent
sandboxes start in about 100 ms.

## Write your own namespace

A namespace is a normal Python project — whatever `uv init --lib` produces —
plus one entry-point block.

=== "Source"

    ```python title="src/agentix/myagent/__init__.py"
    from agentix.namespace import Namespace

    class MyAgent(Namespace):
        """Optional class docstring — surfaces in /namespaces output."""

        @staticmethod
        async def run(instruction: str) -> str:
            # the real implementation — runs inside the sandbox
            return f"did: {instruction}"
    ```

=== "pyproject.toml"

    ```toml title="pyproject.toml"
    [project]
    name = "agentix-myagent"
    version = "0.1.0"

    [project.entry-points."agentix.namespace"]
    myagent = "agentix.myagent:MyAgent"

    [tool.hatch.build.targets.wheel]
    packages = ["src/agentix"]
    ```

=== "Layout"

    ```text
    my-namespace/
    ├── pyproject.toml
    └── src/
        └── agentix/                  # PEP 420 namespace package, no __init__.py
            └── myagent/
                └── __init__.py       # class MyAgent(Namespace)
    ```

!!! warning "Composition over inheritance"
    `MyAgent` subclasses `Namespace` purely for the discovery hook —
    its methods are `@staticmethod` with no `self` and no instance state.
    Do **not** create a separate `MyAgentImpl` that inherits from `MyAgent`.
    If you split an impl from a stub, compose them with
    `Dispatcher.bind_namespace` instead.

Build, bundle, deploy:

```bash
agentix build ./my-namespace                       # → agentix/myagent:0.1.0
agentix install bash myagent -o my-bundle:0.1.0    # bundle several namespaces
agentix deploy local --image my-bundle:0.1.0       # run a sandbox
```

`pip install agentix-myagent` is everything your users need. The framework
discovers the entry point, and `from agentix.myagent import MyAgent`
resolves natively in their code.

## Next

<div class="grid cards" markdown>

-   **[Plugin authors guide](plugins.md)**

    ---

    Deployments, trace sinks, spec resolvers, wire patterns, CLI subcommands —
    all six axes share this same entry-point pattern.

-   **[Architecture](architecture.md)**

    ---

    How the dispatcher, runtime, and wire patterns fit together inside the
    sandbox.

-   **[CLI reference](cli.md)**

    ---

    Every `agentix <subcommand>` documented: `build`, `install`, `deploy`,
    `check`, `plugins`.

-   **[Namespace protocol](namespace-protocol.md)**

    ---

    The wire-format contract between caller and sandbox — useful when
    porting the client to another language.

</div>
