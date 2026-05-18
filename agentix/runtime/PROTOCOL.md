# Agentix RPC Protocol

This file is the runtime wire contract for `RuntimeClient.remote(fn, ...)`.
Tests in `tests/test_rpc_protocol.py` enforce these rules.

## Target

The client derives the remote target from the function object:

```python
target = f"{fn.__module__}::{fn.__name__}"
```

The target is the only function address sent over the wire. Args and
kwargs are sent separately.

Example:

```python
from my_project.tasks import run

await client.remote(run, seed=42)
```

Wire target:

```text
my_project.tasks::run
```

## Transports

| Path | Shape used | Wire |
| --- | --- | --- |
| `POST /_remote` | unary | msgpack body + msgpack response |
| `/socket.io/` | stream + bidi | msgpack-payload events |
| worker stdin/stdout | all three | length-prefixed msgpack frames |

HTTP and Socket.IO are the host-to-runtime edge. Stdin/stdout is the
runtime-to-worker edge inside the sandbox. One worker subprocess serves
all importable modules for a runtime.

## Shapes

`RuntimeClient.remote` and the worker use the same shape rules:

```text
async generator + Channel[T] parameter -> bidi
async generator without Channel[T]     -> stream
everything else                        -> unary
```

`inspect.isasyncgenfunction` is the source of truth for stream/bidi. A
regular `async def` returning an iterator value is unary.

| Shape | Function signature | Client-side return |
| --- | --- | --- |
| unary | `async def f(...) -> T` | `Unary[T]` (awaitable) |
| stream | `async def f(...) -> AsyncIterator[T]: yield ...` | `Stream[T]` |
| bidi | `async def f(..., inbox: Channel[I]) -> AsyncIterator[O]: yield ...` | `Bidi[I, O]` |

## Unary HTTP

Request body:

```python
{
    "target": "my_project.tasks::run",
    "args": [],
    "kwargs": {"seed": 42},
    "call_id": "optional-correlation-key",
}
```

Response body:

```python
{"ok": True, "value": {...}, "error": None}
```

Failures stay in-band:

```python
{"ok": False, "value": None, "error": {...}}
```

The HTTP status remains 200 when the remote function raises or when the
target cannot be imported.

## Socket.IO Events

Stream:

```text
stream       {call_id, target, args, kwargs}
stream:item  {call_id, value}
stream:end   {call_id}
stream:error {call_id, error}
```

Bidi:

```text
bidi:start   {call_id, target, args, kwargs}
bidi:in      {call_id, item}
bidi:end_in  {call_id}
bidi:out     {call_id, value}
bidi:end     {call_id}
bidi:error   {call_id, error}
```

`call_id` correlates events with the in-flight call.

## Worker Frames

Frames between runtime server and worker are length-prefixed msgpack
dicts.

| Direction | Frame | Payload fields |
| --- | --- | --- |
| server -> worker | `CALL` | call_id, kind, target, args, kwargs |
| server -> worker | `BIDI_IN` | call_id, item |
| server -> worker | `BIDI_END_IN` | call_id |
| server -> worker | `CANCEL` | call_id |
| server -> worker | `SHUTDOWN` | none |
| worker -> server | `READY` | none |
| worker -> server | `BOOT_ERROR` | error |
| worker -> server | `RESULT` | call_id, value |
| worker -> server | `STREAM_ITEM` | call_id, value |
| worker -> server | `STREAM_END` | call_id |
| worker -> server | `ERROR` | call_id, error |

## Invariants

1. **One terminal result per call.** Unary ends with `RESULT` or
   `ERROR`. Stream and bidi end with `STREAM_END` or `ERROR`.
2. **Closed calls are quiet.** After a terminal result, later frames for
   the same `call_id` are dropped.
3. **Dual-side validation.** The client serializes args/kwargs through
   pydantic adapters derived from the local function signature. The
   worker validates received args/kwargs against the imported function
   signature before calling it. Return values and stream items are
   serialized by the worker and validated by the client.
4. **Cancellation closes the call.** `CANCEL` causes
   `ERROR(type="Cancelled", cancelled=True)`.
5. **Worker death closes calls.** If the worker exits, the runtime
   worker client fails every in-flight call with
   `ERROR(type="WorkerExited")`.

## Error Model

`ERROR` payload:

```python
{
    "type": "ValueError",
    "message": "...",
    "traceback": "...",
    "cancelled": False,
}
```

Client mapping:

- `cancelled=True` -> `asyncio.CancelledError`
- everything else -> `agentix.RemoteCallError`

Common framework errors:

- `ModuleNotLoaded` — target module is not importable in the runtime venv.
- `FunctionNotFound` — target function name is not present on the module.
- `ValidationError` — args/kwargs failed pydantic validation.
- `SerializationError` — return value or stream item could not be
  serialized.

## Backpressure

- Server-to-client stream items rely on TCP / Socket.IO buffering; emits
  naturally await when buffers fill.
- Client-to-server bidi input uses `Channel(maxsize=N)` on the caller
  side and bounded queues inside the runtime. A slow worker consumer
  propagates back to `await channel.send(item)`.

## Lifecycle

| Edge | Connect | Cleanup |
| --- | --- | --- |
| host -> runtime HTTP | per unary call | httpx closes |
| host -> runtime Socket.IO | lazy on first stream/bidi call | `RuntimeClient.close()` disconnects |
| runtime -> worker | lazy on first remote call | `SHUTDOWN`, wait, terminate/kill fallback |

## Out of Scope

- Per-call timeouts; callers can use `asyncio.wait_for(...)`.
- Retries; calls are at-most-once.
- Auth/TLS; deployments own that layer.
