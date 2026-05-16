<div align="center">

# Agentix

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-agentiix.github.io-blue)](https://agentiix.github.io/)

[Documentation](https://agentiix.github.io/) | [Supported Integrations](#supported-integrations) | [Cookbook](https://github.com/Agentiix/agentix-cookbook) | [LLM Proxy](https://github.com/Agentiix/agentix-llm-proxy) | [Contributing](docs/development.mdx)

</div>

## Overview

**Agentix** is the **execution, tracing, and integration bridge**
between agents and your LLM serving, RL post-training, and evaluation
infrastructure. Whether your agent is a CLI binary
(Claude Code, Codex), a Python framework
([mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent),
[swe-agent](https://github.com/SWE-agent/SWE-agent),
[OpenHands](https://github.com/All-Hands-AI/OpenHands)), or something
you wrote yourself, Agentix hosts it inside an isolated rollout
container, captures every LLM call and tool invocation as a
structured trace, and routes those traces to
[slime](https://github.com/THUDM/slime), custom serving providers,
or benchmark scorers.

Each agent, dataset, or tool is a regular Python project. Call them
from your trainer or evaluator with typed remote dispatch —
`c.remote(fn, ...)` reads `fn`'s signature, so Pyright infers every
return type end-to-end.

## Quickstart

A SWE-bench Verified rollout — clone the repo, run Claude Code, score
the patch — composed from three integrations:

```python
from datasets import load_dataset
from agentix import RuntimeClient, bash, claude_code, swebench

inst = dict(load_dataset("princeton-nlp/SWE-bench_Verified", split="test")[0])

async with RuntimeClient(sandbox.runtime_url) as c:
    await c.remote(
        bash.run,
        command=(
            f"git clone https://github.com/{inst['repo']}.git /testbed && "
            f"cd /testbed && git checkout {inst['base_commit']}"
        ),
    )
    cc = await c.remote(
        claude_code.run,
        instruction=inst["problem_statement"],
        workdir="/testbed",
        env={"ANTHROPIC_API_KEY": api_key},
    )
    diff = await c.remote(
        bash.run, command="cd /testbed && git add -A && git diff --cached",
    )
    s = await c.remote(swebench.score, instance=inst, patch=diff.stdout)
```

## Key Features

- **Run any agent in an isolated rollout container** — a CLI binary
  (Claude Code, Codex), a Python framework (mini-swe-agent,
  swe-agent, OpenHands), or your own. Each integration runs with its
  own dependencies. Built-in recipe:
  [Claude Code](https://github.com/Agentiix/agentix-cookbook/tree/main/claude-code).
- **Score against any benchmark.** Built-in:
  [SWE-bench Verified](https://github.com/Agentiix/agentix-cookbook/tree/main/swebench),
  wrapping the official
  [`swebench`](https://github.com/swe-bench/SWE-bench) harness's test
  specs, log parsers, and grading.
- **Bridge to RL training and serving.** Every LLM call and tool
  invocation streams out as a structured trace via
  [agentix-llm-proxy](https://github.com/Agentiix/agentix-llm-proxy).
  Destinations: [slime](https://github.com/THUDM/slime) (RL data
  buffer), custom LLM providers (serving and evaluation).
- **Pluggable execution backends.** `local` (Docker), `daytona`, and
  `e2b` built in; Fly, Modal, Kubernetes via
  `pip install agentix-deployment-<name>`.
- **Typed remote dispatch.** Call container methods like local
  functions. Three call shapes (unary / server-streaming /
  bidirectional) are auto-detected from your function signature.
- **Trace fan-out.** `agentix.trace.subscribe(fn)` ships every
  integration's `trace.emit(...)` events into OpenTelemetry, Sentry,
  or your own bus — no per-integration wiring.

## Supported Integrations

### Agents

- **Claude Code** — [recipe](https://github.com/Agentiix/agentix-cookbook/tree/main/claude-code)
- **mini-swe-agent / swe-agent / OpenHands / Codex / Aider / your own** —
  wrap with the [agent integration guide](https://agentiix.github.io/integrate-agent);
  contributions welcome.

### Benchmarks

- **SWE-bench Verified** — [recipe](https://github.com/Agentiix/agentix-cookbook/tree/main/swebench),
  built on the official
  [`swebench`](https://github.com/swe-bench/SWE-bench) package's
  `make_test_spec` + `get_eval_report`

### In-tree Primitives

- **bash** — [`primitives/bash`](primitives/bash); shell execution
  inside the rollout container.
- **files** — [`primitives/files`](primitives/files); upload,
  download, and edit files in the rollout container.

### Execution Backends

- `local` — built-in, Docker-based
- `daytona` — built-in
- `e2b` — built-in
- Third-party — `pip install agentix-deployment-<name>`

### RL Frameworks / Serving Providers

- [**slime**](https://github.com/THUDM/slime) — RL post-training;
  traces flow into its data buffer via
  [agentix-llm-proxy](https://github.com/Agentiix/agentix-llm-proxy).
- **Custom LLM providers** — serving and evaluation via
  [agentix-llm-proxy](https://github.com/Agentiix/agentix-llm-proxy).

## Architecture

```
Orchestrator ──HTTP /_remote──► Runtime Server ──fork──► Namespace worker (per integration)
   (trainer)                       (multiplexer)            (own venv, own PATH)
                                        ▲
            Socket.IO /socket.io/ ◄──────┴──── streams, bidi, logs, traces
```

- **Runtime server**: one process per rollout container. Routes
  `POST /_remote` (unary) and Socket.IO events (streams / bidi / logs
  / traces) to per-integration workers spawned lazily on first
  dispatch.
- **Namespace worker**: subprocess that imports the integration using
  its own venv. Each integration's dependencies stay isolated from
  every other's — mix Aider 0.50 and OpenHands 0.20 in one container
  without resolving deps across them.
- **Deployment**: host-side backend (`local`, `daytona`, `e2b`, or a
  third-party plugin) that creates the rollout container and returns
  its `runtime_url`.

Discovery is lazy — a broken integration fails its own calls but
never blocks boot.

## Install

```bash
pip install agentix agentix-bash agentix-files
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
pip install -e primitives/bash -e primitives/files
```

## CLI

```bash
agentix build primitives/bash                              # one integration image
agentix build bash files claude-code -o my-agent:0.1.0     # bundle several
agentix deploy local --image my-agent:0.1.0                # run a rollout container
agentix check                                              # smoke-test every installed integration
```

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

[project.entry-points."agentix.namespace"]
myagent = "agentix.myagent"

[tool.hatch.build.targets.wheel]
packages = ["src/agentix"]
```

After `pip install agentix-myagent`:

```python
from agentix import myagent
result = await c.remote(myagent.run, instruction="...")
```

## Two extension axes

Only things that cross the host↔container boundary need framework-level
discovery:

| Axis | Entry-point group | What it ships | Built-ins |
|---|---|---|---|
| Namespaces | `agentix.namespace` | code that runs **inside the rollout container** | (third-party only) |
| Deployments | `agentix.deployment` | backend that **provisions** the container | `local`, `daytona`, `e2b` |

Host-side hooks (trace pub/sub, spec resolvers, CLI verbs) are plain
Python — `agentix.trace.subscribe(fn)` is the single line that ships
every integration's `trace.emit(...)` events into OpenTelemetry,
Sentry, or your own bus.

## Links

- **Docs site**: [agentiix.github.io](https://agentiix.github.io/)
- **Cookbook**: [github.com/Agentiix/agentix-cookbook](https://github.com/Agentiix/agentix-cookbook)
- **LLM-proxy / RL bridge**: [github.com/Agentiix/agentix-llm-proxy](https://github.com/Agentiix/agentix-llm-proxy)
- **Roadmap**: [ROADMAP.md](ROADMAP.md)
- **Contributing**: [docs/development.mdx](docs/development.mdx); conventions in [CLAUDE.md](CLAUDE.md)

## License

[MIT](LICENSE)
