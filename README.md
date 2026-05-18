<div align="center">

# Agentix

**The bridge between agents, evaluation, RL training, and LLM serving.**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-agentiix.github.io-blue)](https://agentiix.github.io/)

[Documentation](https://agentiix.github.io/) | [Supported Integrations](#supported-integrations) | [Cookbook](https://github.com/Agentiix/agentix-cookbook) | [RL Bridge](https://github.com/Agentiix/abridge)

</div>

## Overview

**Agentix** is the **execution and integration bridge** between agents
and your RL post-training and evaluation infrastructure. It gives
trainers, evaluators, and agent builders one typed Python interface for
running agents, tools, and scorers in isolated rollout containers.

Use it when you need to connect Claude Code, Codex, Aider,
mini-swe-agent, OpenHands, or an in-house agent to SWE-bench or custom
evals without writing a bespoke runner for every agent x benchmark x
training stack.

Each agent, dataset, or tool is a regular Python package. Call it from
your trainer or evaluator with typed remote calls:
`c.remote(fn, ...)` reads `fn`'s signature, so Pyright can infer return
types across the host-to-container boundary.

## Why Agentix

- **One bridge, many stacks.** Shell commands, agent CLIs, Python
  frameworks, dataset scorers, and file operations all use the same
  `RuntimeClient.remote(...)` call path.
- **Bundle without glue sprawl.** User code and installed integrations
  are pip-installed into one runtime venv, then remote functions run in
  one runtime worker subprocess.
- **Training-friendly calls.** Rollout code can compose agents,
  shell/file primitives, and scorers through one typed call path; trace
  capture and LLM proxying live in companion packages and future work.
- **Benchmarks stay composable.** Agent execution and scoring remain
  separate Python modules, which makes it easy to swap agents, scorers,
  and deployment backends independently.

## What Agentix Bridges

| From | Agentix layer | To |
|---|---|---|
| Agent CLIs and Python frameworks | One worker subprocess with typed remote calls | Evaluators, trainers, and orchestration code |
| Shell, file, and tool operations | Shared rollout container surface | Agents and benchmark harnesses |
| Rollout metadata | User-provided call IDs and companion trace bridges | Observability, replay, reward, and dataset pipelines |
| Local and hosted sandboxes | `agentix.deployment` backends | Docker today; Daytona, E2B, and third-party backends as plugins |

## Key Features

- **Run any agent in an isolated rollout container.** Bring a CLI
  binary, a Python framework, or your own package. Cookbook recipe:
  [Claude Code](https://github.com/Agentiix/agentix-cookbook/tree/main/claude-code).
- **Score against any benchmark.** Cookbook recipe:
  [SWE-bench Verified](https://github.com/Agentiix/agentix-cookbook/tree/main/swebench),
  wrapping the official
  [`swebench`](https://github.com/swe-bench/SWE-bench) harness's test
  specs, log parsers, and grading.
- **Pluggable execution backends.** Install `agentix-deployment-docker`
  for local Docker, or backend packages for Daytona, E2B, Fly, Modal,
  Kubernetes via
  `pip install agentix-deployment-<name>`.
- **Typed remote calls across the bridge.** Container functions autocomplete
  like local functions; your editor knows the kwargs and return types.
  Three call shapes (unary / streaming / bidirectional) are
  auto-detected from your function signature.

## Supported Integrations

### Agents

- **Claude Code** — [recipe](https://github.com/Agentiix/agentix-cookbook/tree/main/claude-code)
- CLI binaries (Codex, Aider), Python frameworks
  ([mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent),
  [swe-agent](https://github.com/SWE-agent/SWE-agent),
  [OpenHands](https://github.com/All-Hands-AI/OpenHands)),
  or your own — wrap with the
  [agent integration guide](https://agentiix.github.io/integrate-agent).

### Benchmarks

- **SWE-bench Verified** — [recipe](https://github.com/Agentiix/agentix-cookbook/tree/main/swebench),
  built on the official
  [`swebench`](https://github.com/swe-bench/SWE-bench) package's
  `make_test_spec` + `get_eval_report`

### Sandbox Primitives

- **bash** — shell execution inside the rollout container. Ships with
  [`agentix-runtime-basic`](https://github.com/Agentiix/Agentix-Runtime-Basic).
- **files** — upload, download, and edit files in the rollout container.
  Same wheel.

### Execution Backends

- `local` — Docker-based; ships with
  [`agentix-deployment-docker`](https://github.com/Agentiix/Agentix-Deployment-Docker).
- `daytona` — [`agentix-deployment-daytona`](https://github.com/Agentiix/Agentix-Deployment-Daytona).
- `e2b` — [`agentix-deployment-e2b`](https://github.com/Agentiix/Agentix-Deployment-E2B).
- Third-party — `pip install agentix-deployment-<name>`.

## Architecture

```
Orchestrator ──HTTP /_remote──► Runtime Server ──fork──► Runtime worker
   (trainer)                       (FastAPI + SIO)          (shared venv, own process)
                                        ▲
            Socket.IO /socket.io/ ◄──────┴──── streams, bidi
```

- **Runtime server**: one process per rollout container. Routes
  `POST /_remote` (unary) and Socket.IO events (stream / bidi) to one
  worker subprocess.
- **Runtime worker**: one subprocess that imports requested Python
  modules from the runtime venv and invokes their functions.
- **Deployment**: host-side backend that creates the rollout container
  and returns its `runtime_url`.

## Install

```bash
pip install agentixx \
            agentix-runtime-basic \
            agentix-deployment-docker
```

Cookbook integrations:

```bash
git clone https://github.com/Agentiix/agentix-cookbook
pip install ./agentix-cookbook/claude-code ./agentix-cookbook/swebench
```

Framework development:

```bash
git clone https://github.com/Agentiix/Agentix && cd Agentix
pip install -e '.[dev]'
# Pair with sibling repos checked out next to Agentix/ for a working
# rollout end-to-end:
pip install -e ../Agentix-Runtime-Basic -e ../Agentix-Deployment-Docker
```

## CLI

```bash
agentix build                                                # build current project
agentix build path/to/project -o my-agent:0.1.0              # explicit path + tag
agentix deploy local --image my-agent:0.1.0                  # run a rollout container
```

Multi-plugin bundles are expressed by declaring the plugins as deps
in your project's `pyproject.toml`; pip pulls them in transitively.

## Write an integration

```python
# src/agentix/myagent/__init__.py
async def run(instruction: str) -> str:
    return f"did: {instruction}"
```

```toml
# pyproject.toml
[project]
name = "agentix-myagent"
version = "0.1.0"

[tool.hatch.build.targets.wheel]
packages = ["src/agentix"]
```

After `pip install agentix-myagent`:

```python
from agentix.myagent import run

result = await c.remote(run, instruction="...")
```

## One extension axis

Deployment backends use the `agentix.deployment` entry-point group so
`agentix deploy <backend>` finds them by name. Everything else is just
pip-installable Python — your project depends on `agentix-runtime-basic`
or whatever else, pip resolves it, and the framework probes any
importable module on first remote call.

## Links

- **Docs site**: [agentiix.github.io](https://agentiix.github.io/)
- **Cookbook**: [github.com/Agentiix/agentix-cookbook](https://github.com/Agentiix/agentix-cookbook)
- **RL bridge (abridge)**: [github.com/Agentiix/abridge](https://github.com/Agentiix/abridge)
- **Roadmap**: [ROADMAP.md](ROADMAP.md)
- **Contributing**: [docs/development.mdx](docs/development.mdx); conventions in [CLAUDE.md](CLAUDE.md)

## License

[MIT](LICENSE)
