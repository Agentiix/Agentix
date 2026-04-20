# Roadmap

## v0.1.0 — Closure runtime (current)

Run any Nix closure inside a Docker sandbox, compose multiple closures in one sandbox, expose each over HTTP via a reverse proxy.

- [x] Closure ABI: `VOLUME /nix` + `/nix/store` + `/nix/entry/bin/start`
- [x] `DockerDeployment` — per-image named volume keyed by image digest, auto-populated by Docker; per-closure `-v /mnt/<ns>:ro` + tmpfs `/nix`; sandbox entrypoint builds the `/nix/store` symlink forest and execs the runtime
- [x] Runtime server — built-in `exec / upload / download / ls`, `/closures`, streaming reverse proxy `/{ns}/{path*}`
- [x] Auto-load on startup: runtime scans `/mnt` and forks each closure's `entry/bin/start` (no dynamic `/load`)
- [x] Unit tests + Docker smoke test in CI

Higher-level concepts (agent adapter, dataset runner, benchmark orchestration) are **explicitly out of scope for v0.1.0** and will be revisited once the closure substrate is stable.

## Unscheduled

Future directions, listed so the closure layer is built with them in mind — no committed timelines.

- **LLM proxy** — transparent proxy that intercepts API calls from closures for token-level trajectory capture, cost tracking, replay.
- **Checkpoint / partial rollout** — snapshot a sandbox (filesystem + loaded closure state), fork to explore alternative continuations; enables tree search / RL over execution traces.
- **K8s deployment backend** — parallel `Deployment` implementation using the same closure image contract.
