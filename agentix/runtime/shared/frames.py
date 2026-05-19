"""Wire-frame type tags for `agentix.runtime.shared.framing`.

These are the values of the `type` field in msgpack frames flowing
between the runtime server and worker subprocess over stdin/stdout.
"""

from __future__ import annotations

# ─── runtime → worker frame types ─────────────────────────────────────
CALL = "call"
CANCEL = "cancel"
SHUTDOWN = "shutdown"

# ─── worker → runtime frame types ─────────────────────────────────────
READY = "ready"
BOOT_ERROR = "boot_error"
RESULT = "result"
ERROR = "error"
# A side-channel payload from inside the worker. Today only the trace
# bridge uses it (worker → server → broadcast SIO); the runtime ships
# the inner dict opaquely and has no knowledge of its schema.
TRACE = "trace"
