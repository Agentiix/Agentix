# Agentix

Coding Agent SDK: run ANY agent on ANY environment, collect trajectories for training.

## Architecture

```
Host (Slime / Verl)                 Sandbox
 │                                   │
 │  client.exec(                     │  agentix-server (:8000)
 │    "python -m agentix.eval        │  ├── POST /exec
 │     --agent /opt/agent            │  ├── POST /upload
 │     --dataset /opt/dataset"       │  ├── GET  /download
 │  )                                │  └── GET  /health
 │                                   │
 │  client.download(                 │  agentix.eval CLI
 │    "/output/result.json"          │  └── setup → run → verify
 │  )                                │      → /output/result.json
 │                                   │
 │  Agent plugin    (Nix closure)    │  Dataset plugin  (Nix closure)
 │  ├── bin/claude  (llm-agents.nix) │  └── dataset.py
 │  └── runner.py   (trajectory)     │      setup() → agent_input
 │                                   │      verify() → metrics
```

## Quick Start

```bash
nix develop

# Build
nix build .#runtime
nix build .#claude-code

# Run in Docker
RUNTIME=$(nix build .#runtime --no-link --print-out-paths)
AGENT=$(nix build .#claude-code --no-link --print-out-paths)

docker run -d --name sandbox \
  -v /nix/store:/nix/store:ro \
  -p 8000:8000 ubuntu:24.04 \
  $RUNTIME/bin/agentix-server

# Eval
curl -X POST localhost:8000/exec \
  -H "Content-Type: application/json" \
  -d "{\"command\": \"python -m agentix.eval --agent $AGENT\"}"
```

## Plugins

**Agent plugin** — how to run an agent + collect trajectory:
```
agents/{name}/
├── default.nix    # wraps llm-agents.nix binary + runner.py
└── runner.py      # async def run(agent_input: dict) -> RunResult
```

**Dataset plugin** — environment setup + verification:
```
datasets/{name}/
├── default.nix
└── dataset.py     # async def setup() -> dict
                   # async def verify() -> dict
```

## Structure

```
agentix/
├── runtime/          server + client (sandbox interface)
├── datasets/         dataset plugin protocol
├── agents/           agent plugin protocol
├── trajectory.py     ATIF v1.4 format
├── eval.py           eval CLI (setup → run → verify)
└── models.py
```

## Docs

- [Architecture](docs/architecture.md)
- [Agent Protocol](docs/agent-protocol.md)
- [Development](docs/DEVELOPMENT.md)
