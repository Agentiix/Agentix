# Roadmap

## v0.1.0 — RPC + bundle (current)

Two concepts, no more:

- **RPC.** `c.remote(fn, ...)` calls any importable Python function
  in a sandboxed worker subprocess. `unary`, server-`stream`, and
  `bidi` shapes are detected from the function signature; the wire
  flows over HTTP for unary and Socket.IO for the rest.
- **Bundle.** `agentix build [path]` packages one project root + its
  declared deps into a deploy-ready Docker image. Integrations arrive
  transitively via pip; the runtime imports target modules on demand.

What's shipped:

- [x] Function invocation + worker subprocess (`agentix.invoke`,
      `agentix.runtime.server.worker`).
- [x] HTTP + Socket.IO transport (`agentix.runtime.shared.codec` /
      `events` / `rpc`).
- [x] `DockerDeployment` (lives in `agentix-deployment-docker`).
- [x] Single-spec `agentix build` — one project root, integrations via pip.
- [x] On-demand module import — user projects don't need entry points.
- [x] Merged-only bundle — one shared `/nix/runtime/` venv. Integrations
      and user code coexist in one venv.

Sibling repos (each independently releasable):

- [`Agentix-Runtime-Basic`](https://github.com/Agentiix/Agentix-Runtime-Basic)
  — `bash` + `files` modules. On PyPI as `agentix-runtime-basic`.
- [`Agentix-Deployment-Docker`](https://github.com/Agentiix/Agentix-Deployment-Docker)
  — local Docker backend. On PyPI as `agentix-deployment-docker`.
- [`Agentix-Deployment-Daytona`](https://github.com/Agentiix/Agentix-Deployment-Daytona),
  [`Agentix-Deployment-E2B`](https://github.com/Agentiix/Agentix-Deployment-E2B)
  — stub backends; CLI surface in place, lifecycle wiring pending.
- [`abridge`](https://github.com/Agentiix/abridge) — host-side
  rollout-to-RL-buffer bridge.

## Unscheduled

Future directions, listed so the framework is built with them in mind.

- **Trace pub/sub** — remote functions emit structured events, and
  subscribers receive rollout-scoped fan-out.
- **RolloutPool** — warm sandbox pool for batched RL rollouts.
- **LLM proxy** — transparent proxy that intercepts API calls from
  remote functions for token-level trajectory capture, cost tracking,
  and replay.
- **Checkpoint / partial rollout** — snapshot a sandbox (filesystem +
  loaded worker state), fork to explore alternative continuations.
  Enables tree search / RL over execution traces.
- **K8s deployment backend** — parallel `Deployment` implementation
  using the same bundle-image contract; would ship as
  `agentix-deployment-k8s`.
