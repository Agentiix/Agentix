# Plugin System Research: Lessons from 6 Mature Systems

Survey of plugin architectures to inform Agentix refactoring.
Each system is analyzed across 5 dimensions: discovery, lifecycle, schema/contracts,
error handling, and composition.

---

## 1. pytest / pluggy

**What it is:** Python's dominant test framework. pluggy is the standalone hook system
extracted from pytest — a general-purpose plugin framework.

**Discovery:**
- `pytest11` entry_points in installed packages (pip-based)
- `conftest.py` auto-discovered in test directories
- CLI `-p` flag and `PYTEST_PLUGINS` env var

**Lifecycle hooks:**
```python
# Hook specification — defines the contract
@hookspec
def pytest_runtest_setup(item): ...

# Hook implementation — plugin provides behavior
@hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # runs before other implementations
    pass

# firstresult — stops at first non-None return
@hookspec(firstresult=True)
def pytest_configure(config): ...
```

**Schema enforcement:**
- Validates hook impl signatures match hookspec at `pm.register()` time
- Mismatch raises `PluginValidationError` immediately — not at call time
- Parameters must be a subset of the spec (extra params rejected)

**Error handling:**
- `PluginValidationError` on bad signatures
- `HookCallError` on insufficient arguments at call time
- Hook wrapper teardown errors issue `PluggyTeardownRaisedWarning`

**Relevance to Agentix:**
- pluggy validates function *signatures*, but Agentix plugins all share the same
  signature `run(ctx) -> dict`. The problem is validating ctx *contents*, not signatures.
- Hook ordering (tryfirst/trylast) is unnecessary — Agentix has a fixed 3-phase pipeline.
- Entry_points discovery requires pip install — incompatible with Nix closures.
- **Takeaway: pluggy is over-engineered for Agentix's fixed pipeline. Skip hookspec/hookimpl.**

---

## 2. Terraform Providers

**What it is:** Infrastructure-as-code tool. Providers are plugins that manage
specific cloud/service APIs via a well-defined protocol boundary.

**Discovery:**
- `required_providers` block in HCL declares source + version constraints
- `terraform init` downloads from Terraform Registry
- Local cache at `.terraform/providers/`
- Versions locked in `.terraform.lock.hcl`

**Lifecycle:**
- Core orchestrates: init → plan → apply → destroy
- Providers respond to gRPC calls: Create, Read, Update, Delete, Import, Validate
- Each provider runs as a separate subprocess (process isolation)

**Schema/contracts — the key insight:**
```
Required    — user must provide, provider never sets
Optional    — user may provide, has a default
Computed    — provider sets, user cannot
Optional+Computed — user may set, provider fills if omitted
ForceNew    — changing this field triggers resource recreation
```

This trichotomy maps directly to Agentix's ctx flow:
- `requires` = plugin needs this key from upstream (dataset.setup or host)
- `optional` = plugin uses if present, has sensible default
- `provides` = plugin produces this key (output for downstream)

**Protocol:**
- Protocol Buffers + gRPC over HTTP2
- Type safety enforced through protobuf schema
- Core and provider are separate binaries — language-agnostic

**Relevance to Agentix:**
- **Schema model is the best fit for E1.** Required/Optional/Computed maps perfectly
  to ctx key contracts between phases.
- gRPC protocol boundary is overkill — Agentix plugins run in-process via importlib,
  with sandbox providing process isolation.
- Version pinning via lock file is already handled by Nix's flake.lock.
- **Takeaway: Borrow the schema model. Skip the protocol boundary.**

---

## 3. Flask / FastAPI Extensions

**What it is:** Python web frameworks with different extension philosophies.

**Discovery:**
- Flask: import-based (`flask_*` naming convention), entry_points, `app.register_blueprint()`
- FastAPI: explicit `app.add_middleware()`, no built-in discovery

**Lifecycle:**
```python
# Flask: init_app pattern — deferred binding
class MyExtension:
    def __init__(self): pass
    def init_app(self, app):
        app.config.setdefault('MY_KEY', 'default')

# FastAPI: lifespan context manager — setup/teardown in one place
@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await connect_db()   # startup
    yield {"db": db}          # app runs
    await db.close()          # shutdown (always runs)

app = FastAPI(lifespan=lifespan)
```

**Contracts:**
- Flask: config-driven, extensions declare defaults via `app.config.setdefault()`
- FastAPI: dependency injection system, ASGI interface compliance

**Error handling:**
- Flask: load failure prevents app init (fail-fast)
- FastAPI: startup errors (before yield) prevent app start; shutdown errors logged but don't block

**Relevance to Agentix:**
- **FastAPI's lifespan pattern maps to setup→run→teardown.** The context manager
  guarantees teardown runs even on error — cleaner than explicit try/finally.
- Flask's init_app deferred binding isn't needed (plugins don't bind to an app object).
- **Takeaway: Use the context manager teardown pattern for E2. Skip init_app.**

---

## 4. Kubernetes CRDs + Operators

**What it is:** Container orchestration platform. CRDs extend the API; operators
implement custom reconciliation logic.

**Discovery:**
- Controllers use informers (client-side caches) backed by API server watches
- Work queues decouple event detection from reconciliation processing

**Lifecycle — the reconcile loop:**
```
Observe  → Watch for resource changes via informers
Diff     → Compare current state vs desired state
Act      → Take corrective action (create/update/delete)
Requeue  → On failure, requeue with exponential backoff
```

This loop runs continuously — no "done" state, just convergence.

**Schema:**
- CRDs define OpenAPI v3 structural schemas
- Validation happens server-side at admission time (before persistence)
- Field-level validation rules (min/max, pattern, enum)

**Plugin interfaces (CSI/CNI):**
- gRPC over UNIX socket for storage (CSI) and networking (CNI)
- Well-defined RPC boundary — plugins are separate binaries
- Sidecar containers watch K8s resources and trigger plugin calls

**Error handling:**
- Reconcile failures trigger automatic requeue with backoff
- No explicit retry code needed — the framework handles it
- Transient failures converge naturally through repeated reconciliation

**Relevance to Agentix:**
- **Reconcile pattern (diff → act) is the right model for E3** (sandbox update).
  Instead of delete+recreate, diff the config: if only agent_closure changed,
  use `docker cp` + restart. Only recreate when the base image changes.
- **Requeue-with-backoff is the right retry model for R1** (RuntimeClient retries).
- OpenAPI schema validation at admission = validating ctx at eval boundary.
- **Takeaway: Borrow reconcile-diff for updates, requeue-with-backoff for retries.**

---

## 5. Gradle Plugins + VSCode Extensions

**What it is:** Build tool (Gradle) and editor (VSCode) with rich plugin ecosystems.

**Gradle discovery:**
- Plugin Portal (remote registry), `buildSrc` (local auto-compiled), included builds
- Plugins map implementation classes to IDs via `java-gradle-plugin`

**Gradle lifecycle — 3 phases:**
```
Initialization  → settings.gradle evaluated, projects discovered
Configuration   → build scripts execute, plugins apply, task graph built
Execution       → tasks run in dependency order (DAG)
```
Hooks: `settingsEvaluated`, `projectsLoaded`, `beforeProject`, `afterProject`, `projectsEvaluated`

**Gradle extension model:**
```groovy
// Plugin exposes configuration via extensions
project.extensions.create("myConfig", MyConfigClass)
// Users configure with DSL blocks
myConfig { property = "value" }
```

**VSCode discovery — manifest-driven:**
```json
// package.json
{
  "activationEvents": ["onLanguage:python", "workspaceContains:*.py"],
  "contributes": {
    "commands": [{"command": "ext.run", "title": "Run"}],
    "configuration": { "properties": { "ext.timeout": { "type": "number" } } }
  }
}
```

**VSCode lifecycle:**
```typescript
export function activate(context: vscode.ExtensionContext) { ... }
export function deactivate() { ... }  // optional cleanup
```

**Relevance to Agentix:**
- **VSCode's `package.json` manifest is the best discovery model for E4.** A static
  JSON file declares plugin metadata without importing code. No registration step,
  no entry_points boilerplate, no pip install.
- Gradle's task DAG is overkill — Agentix has a linear pipeline, not a dependency graph.
- VSCode's `activationEvents` (lazy loading) isn't needed — plugins load once per eval.
- **Takeaway: Borrow VSCode's manifest pattern. Skip Gradle's DAG and extension DSL.**

---

## 6. NixOS Module System

**What it is:** NixOS's configuration system — a declarative, typed, composable
plugin architecture for system configuration.

**Module structure:**
```nix
{ config, pkgs, ... }:
{
  imports = [ ./other-module.nix ];   # discovery via explicit imports

  options.myService.port = mkOption {  # typed option declaration
    type = types.int;
    default = 8080;
    description = "HTTP port";
  };

  config = mkIf config.myService.enable {  # conditional configuration
    systemd.services.myService = { ... };
  };
}
```

**Typed options:**
- `types.str`, `types.int`, `types.listOf types.str`, `types.enum ["a" "b"]`
- `types.submodule` for nested plugin configurations
- Types define both `check` (validate) and `merge` (combine multiple definitions)

**Merging:**
- Lists concatenate, attrsets merge recursively
- `mkDefault` (priority 1000), `mkForce` (priority 50) for conflict resolution
- `mkBefore` / `mkAfter` for ordering within lists

**Discovery:**
- Explicit `imports` list — no auto-scanning, no magic
- Full dependency graph evaluated lazily

**Relevance to Agentix:**
- Agentix already uses Nix for packaging — the module system is a natural fit
  for *Nix-level* plugin composition (flake.nix already does this).
- But for *Python-level* plugin contracts (ctx schema), NixOS modules are too heavy.
  The merge semantics (mkForce, mkBefore) add complexity that a flat ctx dict doesn't need.
- **Takeaway: Keep using Nix modules for packaging. Use simpler Python-native schemas
  for runtime contracts.**

---

## Summary: What to Borrow, What to Skip

| Agentix Issue | Borrow From | Pattern | Skip |
|---------------|-------------|---------|------|
| **E1: ctx contracts** | Terraform | Required/Optional/Computed schema as plain dict | pluggy signature validation, NixOS type merging |
| **E2: lifecycle hooks** | FastAPI | Context manager or explicit teardown/on_error | pluggy hookspec/hookimpl, Gradle phase hooks |
| **E3: sandbox update** | Kubernetes | Reconcile loop: diff config → act on delta | Terraform plan/apply (too heavy) |
| **E4: discovery** | VSCode | Static manifest.json + directory scanning | pytest entry_points (requires pip), Gradle portal |
| **R1: retries** | Kubernetes | Requeue with exponential backoff | — |
| **General** | All | Validate at boundary, not inside plugins | Forcing framework imports on plugin authors |
