# Project conventions

## No backward compatibility

This repo is in active design. **Breaking changes are fine; do not introduce backward-compat shims.**

- **No aliases.** Rename `foo` → `bar`: delete `foo`, don't accept both.
- **No deprecation warnings.** Delete the thing.
- **No `// removed ...` / `// kept for compat` comments.** Git history covers that.
- **No version-bump fences.** Update code, docs, tests, move on.
- **Tests:** update them to the new shape; don't keep a test that exercises removed behavior.

Downstream repos (`Agentix-Agents-Hub`, `Agentix-Datasets`) are updated in lockstep — assume they follow HEAD.

## Architecture (modular Nix-closure composition)

The inspiration: a single `/nix` Docker volume can be mounted into any task container and provide an entire runtime via Nix's content-addressed store. We modularize that idea — **many closure images, each a `/nix` slice, composed by the sandbox runtime**.

### Closure image convention

Every closure image satisfies exactly:

- `VOLUME /nix`
- `/nix/store/<hash>-*/` — content-addressed Nix deps
- `/nix/entry/bin/start` — executable entry point

`start` takes no CLI args. Runtime passes `AGENTIX_SOCKET` (path to the Unix socket to bind) via env.

The runtime's own "closure" is just another image satisfying the same convention; its `start` launches `agentix-server`.

### Sandbox layout at runtime

```
/nix/                     — tmpfs (writable by entrypoint only)
  store/                  — symlink forest: each /mnt/<ns>/store/<hash> linked here
/mnt/
  runtime/                — -v agentix-closure-<runtime-key>:/mnt/runtime:ro
    store/<hash>-*/
    entry/bin/start       — agentix-server
  <ns>/                   — -v agentix-closure-<closure-key>:/mnt/<ns>:ro
    store/<hash>-*/
    entry/bin/start
```

Sandbox entrypoint (inlined into the `docker run` command):
```sh
mkdir -p /nix/store
for d in /mnt/*/store; do ln -sfn "$d"/* /nix/store/; done
exec /mnt/runtime/entry/bin/start
```

### Deployment flow

1. **First time a closure image is seen**: `docker run --rm -v agentix-closure-<digest>:/nix <image> true`. Docker's volume-init-from-image rule auto-populates the named volume from the image's `/nix` layer on first attach; skips if already populated. The volume key is the image's SHA256 digest, so rebuilds produce a fresh volume automatically.
2. **Sandbox create**: `-v agentix-closure-<digest>:/mnt/<ns>:ro` per closure + `--tmpfs /nix` + entrypoint above.

Closures are fixed at sandbox creation — no dynamic load/unload. The runtime's lifespan scans `/mnt` and forks every closure it finds.

### Closure fork

For each `/mnt/<namespace>/entry/bin/start` the runtime discovers, it forks with:
- `PATH=/mnt/<namespace>/entry/bin:<scrubbed PATH>`
- `AGENTIX_SOCKET=/tmp/agentix/<namespace>.sock`

### PATH policy for `/exec`

User subprocess default `PATH=/usr/local/bin:/usr/bin:/bin` (task image's). Nix env vars (`LD_LIBRARY_PATH`, `NIX_*`, `PYTHONPATH`, etc.) scrubbed to avoid ABI clash. `paths_from=["<ns>"]` prepends `/mnt/<ns>/entry/bin`.

### What Nix buys us

- Content-addressed `/nix/store` paths → multiple closures' deps never collide, so the symlink forest is trivially safe
- Hermetic per-closure deps → each closure's `bin/start` references its own `/nix/store/*` via Nix-absolute shebangs + RPATH, no PATH pollution between closures

### Deliberate non-choices

- **No monolithic single-image runtime**: we are many small images, each a focused slice.
- **No global PATH merge.** Each closure's PATH is scoped to its own `/mnt/<ns>/entry/bin`.
- **No `--volumes-from` into the sandbox.** We use `-v named-volume:/mnt/<ns>:ro` per closure so each mounts at a unique path.

## Implementation notes

- **Hash paths are internal.** Users pass Docker image refs. The `/nix/store/<hash>-...` path only surfaces in `GET /closures` and debugging.
- **No local Nix required.** Closure authors do `docker build`; Nix lives in the builder stage of their Dockerfile.
- **Sandbox starts fast** once closures are populated: a warm sandbox is `-v` mounts + tmpfs + symlink loop (shell-time, ~100 ms) + runtime boot.
- **Populate is lock-serialised** in-process to avoid concurrent `docker run -v` races on the same image's volume. Cross-process coordination is not currently provided; documented as a single-orchestrator assumption.
