"""Per-call request-id propagation between capture and transport layers.

The tunnel deliberately carries no HTTP metadata (see `proxy.Request`), so a
request id can't ride the `Request` object. But capture and transport must
agree on ONE id per call: the `Recorder` writes a `request_id` into its JSONL
row, and the transport layer (`Forward`, the SDK clients) stamps
`x-request-id` on the upstream hop — downstream token recorders (the TITO
gateway) echo that header into their own per-turn records. If each layer
minted its own id, the message-level row and the token-level record for the
same call could never be joined.

A `ContextVar` is the seam: the outermost interested layer (the `Recorder`,
when present) mints the id and binds it for the duration of the handler call;
inner layers reuse a bound id and only mint their own when nothing upstream
bound one. Works unchanged across `await` within one handler invocation and
never leaks across concurrent calls.

`current_upstream_session_id` flows the OTHER way on the same principle: the
transport layer (`Forward`) publishes the session id it stamped upstream as
`x-session-id` — for a `SessionForward` that is the gateway-assigned session
id, which the caller-side capture cannot otherwise know (it exists only
after the lazy session create). The `Recorder` clears it before each handler
call and reads it afterwards into the row's `gateway_session_id`, restoring
the session-level join between caller-side rows and gateway-side records.

`current_response_message` flows the same way and carries the STRUCTURE the
wire loses. An agent that asked for streaming gets `text/event-stream` bytes
back, so a capture layer reading only `ClientResponse` would have to re-parse
an SSE blob to find the assistant's `thinking` / `text` / `tool_use` blocks —
exactly the kind of string-scraping this capture exists to eliminate. The
Anthropic-face clients already hold the completed `Message` dict at that
point (they drain the stream and re-render it), so they publish it here and
the `Recorder` writes the object, not the blob.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

current_request_id: ContextVar[str | None] = ContextVar("abridge_request_id", default=None)

current_upstream_session_id: ContextVar[str | None] = ContextVar(
    "abridge_upstream_session_id", default=None
)

current_response_message: ContextVar[dict[str, Any] | None] = ContextVar(
    "abridge_response_message", default=None
)


def mint_request_id() -> str:
    return uuid.uuid4().hex


def get_or_mint_request_id() -> str:
    """The id bound by an outer capture layer, or a fresh one."""
    bound = current_request_id.get()
    return bound if bound else mint_request_id()


def publish_response_message(message: dict[str, Any]) -> None:
    """Hand the completed provider-native response to an outer capture layer.

    A no-op when nothing is capturing — setting a `ContextVar` nobody reads
    costs one assignment, so clients call this unconditionally rather than
    branching on whether a `Recorder` happens to be installed.
    """
    current_response_message.set(message)


__all__ = [
    "current_request_id",
    "current_response_message",
    "current_upstream_session_id",
    "get_or_mint_request_id",
    "mint_request_id",
    "publish_response_message",
]
