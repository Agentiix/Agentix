# Architecture Review: Issues & Refactoring Targets

## Extensibility

### E1: Implicit ctx contracts
`ctx: dict` creates invisible coupling between plugins. No schema validation, no
discoverability. Key collisions and missing-key bugs only surface at runtime.

### E2: No plugin lifecycle hooks beyond setup/run/verify
No `teardown()` or `on_error()`. Resource leaks if `run()` crashes mid-execution.

### E3: update() is delete+recreate
`DockerDeployment.update()` destroys all sandbox state. No in-place update path.

### E4: No plugin discovery/registry
Plugins loaded by absolute path only. No `list-agents`, no manifest, no batch
orchestration support.

## Reliability

### R1: No retries in RuntimeClient
All methods fail on first HTTP error. Network blips = unrecoverable failure.

### R2: Racy port allocation
`self._next_port += 1` is not safe under concurrency. No check for port availability.

### R3: exec() buffers entire stdout/stderr in memory
No output size limit. A misbehaving agent can OOM the runtime server.

### R4: No authentication on runtime server
Any local process can hit /exec and run arbitrary commands in the sandbox.

### R5: Upload path traversal
`executor.upload()` writes to any absolute path. Relies entirely on sandbox isolation.

### R6: No overall eval timeout
`run_eval()` can hang forever if `runner.run()` enters an infinite loop.

## Developer Friendliness

### D1: Opaque plugin loading errors
Syntax errors or missing `run()` produce raw importlib tracebacks. No guidance.

### D2: No example dataset plugin
Only agent example exists. Dataset authors have only type hints to work from.

### D3: No dry-run / validation tooling
No way to check a plugin loads correctly without running the full eval.

### D4: Minimal logging
Three `logger.info()` calls in the eval flow. No timing, no run IDs, no structured logs.

### D5: No tests
Empty tests directory. No unit or integration tests.
