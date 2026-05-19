"""Socket.IO event-name constants — the single source of truth for the
strings on the wire between RuntimeClient and the runtime server.

Both `agentix.runtime.server.sio` and `agentix.runtime.client.client`
import from here. Typing one of these strings inline in either file
risks a silent client/server mismatch.

Naming convention: the constant name mirrors the event-name string
(`CALL_RESULT` → `"call:result"`).
"""

from __future__ import annotations

# A remote call: one request → one return value (or one error).
CALL = "call"
CALL_RESULT = "call:result"
CALL_ERROR = "call:error"

# Cancel an in-flight call by call_id.
CANCEL = "cancel"

# Trace stream — server broadcasts every worker-emitted trace frame to
# all connected sessions as `trace:event`. There is no `subscribe` —
# clients receive everything from the moment they connect.
TRACE_EVENT = "trace:event"
