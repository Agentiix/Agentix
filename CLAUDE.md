# Project Conventions

## Two Concepts

Agentix has exactly two ideas:

1. **Remote calls** — `c.remote(fn, *args, **kwargs)` calls an
   importable Python function inside a sandbox worker. The target is
   `fn.__module__ + "::" + fn.__name__`; the call shape (unary /
   stream / bidi) is detected from the function signature; the return
   value is decoded into `fn`'s return type.
2. **Bundle** — `agentix build [path]` packages a Python project and
   its declared dependencies into a deploy-ready Docker image. The
   project's `[project].dependencies` defines what modules are
   installed into the runtime venv.

The primary user model is:

```python
from app import run

result = await client.remote(run, input="hello")
```

`import app; await client.remote(app.run, ...)` also works because it
passes the same function object.

## Composition Over Inheritance

Use inheritance only for genuine lifecycle interfaces such as a
deployment backend implementing the `Deployment` Protocol. Everywhere
else, prefer normal functions, Protocols, composition objects, or
callbacks.

A remote target is just a Python module exposing functions. There is no
base class for user code to inherit from and no marker Protocol for
users to import.

## No Backward Compatibility Shims

This repo is in active design. Breaking changes are fine.

- Rename by deleting the old name, not by accepting both.
- Do not add deprecation warnings.
- Do not leave comments explaining removed behavior.
- Update tests to the current shape; do not preserve tests for removed
  behavior.

Sibling repos (`Agentix-Runtime-Basic`, `Agentix-Deployment-*`,
`agentix-cookbook`) are updated in lockstep with HEAD.

## Systems Map

```text
agentix/
├── invoke/              — internal function binding + call-shape detection
│   ├── shape.py             — detect_shape (unary | stream | bidi)
│   ├── bound.py             — _BoundMethod + arg coercion helper
│   └── invoker.py           — FunctionInvoker
├── runtime/
│   ├── shared/              — wire types, codec, framing, event names
│   ├── client/              — RuntimeClient
│   └── server/              — FastAPI + Socket.IO + worker client + worker
├── deployment/          — Deployment Protocol + backend plugin loader
└── cli/                 — agentix build, agentix deploy
```

One line per system:

- **invoke** — resolves a function name on an imported module, compiles
  pydantic adapters once, validates args, calls the function, and
  serializes outputs.
- **runtime.shared** — msgpack codec, length-prefixed worker frames,
  Socket.IO event names, pydantic wire models, and branded wire ids.
- **runtime.client** — `RuntimeClient.remote(fn, ...)`; HTTP for unary,
  Socket.IO for stream and bidi.
- **runtime.server** — `agentix-server`; owns one runtime worker
  process, forwards HTTP/Socket.IO calls, and correlates events by
  `call_id`.
- **deployment** — host-side `Deployment` Protocol and backend lookup
  for `agentix deploy <backend>`.
- **cli** — `agentix build [path]` and `agentix deploy <backend>`.

## Remote Call Implementation

`c.remote(fn, ...)` reads exactly two attributes of `fn`:

```python
target = f"{fn.__module__}::{fn.__name__}"
```

Example:

```python
# my_project/tasks.py
async def run(seed: int) -> dict:
    ...

# caller
from my_project.tasks import run

result = await client.remote(run, seed=42)
```

The HTTP/Socket.IO payload carries:

```python
{
    "target": "my_project.tasks::run",
    "args": [],
    "kwargs": {"seed": 42},
    "call_id": "optional-correlation-key",
}
```

The worker splits the target, imports `my_project.tasks`, resolves
`run`, validates args with pydantic, calls the function, and serializes
the result.

## Call Shapes

Three shapes are detected from `fn`'s signature:

- `async def f(...) -> T` -> **unary**
- `async def f(...) -> AsyncIterator[T]: yield ...` -> **stream**
- `async def f(..., inbox: Channel[I]) -> AsyncIterator[T]` -> **bidi**

`c.remote(...)` returns `Unary[T]`, `Stream[T]`, or `Bidi[I, T]`.
Await unary; `async for` over stream and bidi.

Sync functions work for unary too; the invoker awaits only when the
result is awaitable. Streams and bidi require async generators.

## Bundle Implementation

`agentix build [path]` packages one project root into a deploy-ready
image. The CLI does not enumerate runtime integrations; they arrive
through pip from `[project].dependencies`.

```toml
[project]
name = "my-agent"
version = "0.1.0"
dependencies = [
    "agentixx>=0.1.0",
    "agentix-runtime-basic>=0.1.0",
    "agentix-deployment-docker>=0.1.0",
]
```

Build stages:

1. Optional Nix stage if the project has `default.nix`; system binaries
   are copied into the final image and linked under `/nix/runtime/bin`.
2. Final image from `agentix/runtime:<version>`; copy the project and
   run one `pip install /src/project` into `/nix/runtime`.

The result is one shared `/nix/runtime` venv. User code, runtime
integrations, direct dependencies, and transitive dependencies are all
importable by the worker.

## Wire Protocol

Unary uses `POST /_remote`:

```text
request  msgpack({target, args, kwargs, call_id})
response msgpack({ok, value, error})
```

Stream and bidi use Socket.IO events:

```text
stream       {call_id, target, args, kwargs}
stream:item  {call_id, value}
stream:end   {call_id}
stream:error {call_id, error}

bidi:start   {call_id, target, args, kwargs}
bidi:in      {call_id, item}
bidi:end_in  {call_id}
bidi:out     {call_id, value}
bidi:end     {call_id}
bidi:error   {call_id, error}
```

Errors stay in-band: HTTP remains 200 for unary, and Socket.IO emits an
error event for stream and bidi.
