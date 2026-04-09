<div align="center">

# Agentix

### Run Any Agent on Any Benchmark

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/Agentiix/Agentix)](https://github.com/Agentiix/Agentix)

</div>

Agentix packages coding agents as reproducible **Nix closures** and injects them into any benchmark's Docker image — SWE-bench, OpenSWE, OS-World, and more. One agent build, every benchmark.

**Any Agent** — Claude Code, Codex, Aider, SWE-agent, OpenHands.
**Any Benchmark** — SWE-bench, SWE-bench Pro, OpenSWE, OS-World, HumanEval.
**Reproducible** — Same git commit = same binaries, forever.
**Deployment Agnostic** — Docker, Kubernetes, Modal, E2B.

## Quick Start

```bash
# Build
RUNTIME=$(nix build .#runtime --no-link --print-out-paths)
AGENT=$(nix build .#claude-code --no-link --print-out-paths)

# Inject into any benchmark Docker image
docker run -d --name sandbox \
  -v /nix/store:/nix/store:ro \
  -e PATH=$AGENT/bin:$RUNTIME/bin:/usr/bin:/bin \
  -p 8000:8000 \
  ubuntu:24.04 \
  $RUNTIME/bin/agentix-server

# Run agent
curl -X POST localhost:8000/exec \
  -H "Content-Type: application/json" \
  -d '{"command": "claude -p \"Fix the bug in main.py\" --output-format text"}'

# Retrieve results
curl "localhost:8000/download?path=/workspace/main.py"
```

## How It Works

Each agent is a **Nix closure** (binary + deps + adapter), injected into containers via volume mount:

```bash
-v /nix/store:/nix/store:ro   # mount closures read-only
-e PATH=$AGENT/bin:...        # expose agent binary
```

The **runtime server** inside the sandbox provides a universal HTTP interface:

| Endpoint | Purpose |
|----------|---------|
| `POST /exec` | Execute commands |
| `POST /upload` | Upload files |
| `GET /download` | Download files |
| `GET /health` | Health check |

The **agent adapter** (`runner.py`) calls the CLI binary and returns structured output:

```python
async def run(agent_input: AgentInput) -> AgentOutput:
    # AgentInput:  instruction, workdir, env
    # AgentOutput: exit_code, stdout, stderr, trajectory
```

## Repositories

| Repo | Purpose |
|------|---------|
| **[Agentix](https://github.com/Agentiix/Agentix)** | Core — runtime server, deployment, agent protocol |
| **[Agentix-Agents-Hub](https://github.com/Agentiix/Agentix-Agents-Hub)** | Agent adapters — claude-code, aider, ... |
| **[Agentix-Datasets](https://github.com/Agentiix/Agentix-Datasets)** | Benchmark runners — SWE-bench, ... |

## Roadmap

| Phase | Focus | Status |
|-------|-------|--------|
| **0** | Agent evaluation on benchmarks | In Progress |
| **1** | LLM Proxy — token-level trajectory tracing | Planned |
| **2** | Partial Rollout — search & RL over trajectories | Planned |

See [ROADMAP.md](ROADMAP.md) for details.
