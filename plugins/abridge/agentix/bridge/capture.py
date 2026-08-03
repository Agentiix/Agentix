"""`abridge.record.v1` — the normative capture shape for the Anthropic face.

The OTel GenAI span the bundled clients stamp (`_genai_span.py`) records a
call as `(role, content-flattened-to-text)` pairs plus token counts. That is
enough to *watch* a rollout and not nearly enough to *reconstruct* one: the
tool schemas are gone, `thinking` signatures are gone, a `tool_use` block
becomes a printf'd string, and nothing says whether the context was rewritten
between two calls. This module defines the record that closes that gap, and
`recorder.py` writes it.

**This module docstring is the normative schema document** — downstream
consumers adapt to the shape defined here (the same contract stance the token
gateway's `tito.record.v1` takes).

## Capture levels

Verbatim bodies are large and contain whatever the user typed, so full
capture is **opt-in** and the ladder is strictly additive:

`off`
    Nothing — no recorder is installed.
`metadata`
    Identity, model, sampling, usage, tool NAMES, per-message digests, the
    response block skeleton, and the prefix relation. No conversation text.
`verbatim`
    Everything above **plus** the complete request body (`system`, `tools`
    with full schemas, the entire message history) and the complete
    structured response (`thinking` + its opaque `signature`, `text`,
    `tool_use`).

`metadata` is the default: an operator who turns recording on gets the join
keys and the rewrite signal without their users' prompts landing on disk.
Nothing silently promotes a configured level.

`shape`, `sampling`, `conversation_key`, and `prefix` are derived from the
Anthropic Messages body, so they appear only on `MESSAGES_PATH` rows. A
`Recorder` wrapping some other face still captures that face's bodies at the
verbatim level; it just reports no Anthropic-face derivations for them.

## The record

One JSON line per served call::

    {
      "schema_version": "abridge.record.v1",
      "session_id": "...",            // OPTIONAL: only when the Recorder has one
      "gateway_session_id": "...",    // OPTIONAL: only when the transport published one
      "request_id": "<32 hex>",       // == the x-request-id stamped upstream
      "turn_index": 0,                // monotonic per file; gaps mark dropped rows
      "ts": 1750000000.0,
      "path": "/v1/messages",
      "capture_level": "verbatim",    // the level ACTUALLY applied to this row
      "model": "claude-sonnet-4-5",   // the model field the agent sent
      "stream": true,                 // what the agent asked for
      "sampling": {"max_tokens": 8192, "thinking": {...}, ...},
      "conversation_key": "<16 hex>", // lane id — see "Prefix relation"
      "shape": {
        "system_digest": "<16 hex>",  // null when the request carried no system
        "tools_digest": "<16 hex>",   // over the FULL schemas, not the names
        "tool_names": ["Bash", "Agent", ...],
        "messages": 42,
        "message_digests": ["<16 hex>", ...],   // one per message, in order
        "content_blocks": [                     // what the model emitted
          {"type": "thinking", "chars": 0, "signature_chars": 210},
          {"type": "text", "chars": 412},
          {"type": "tool_use", "name": "Bash", "input_chars": 88}
        ],
        "response_digest": "<16 hex>"
      },
      "prefix": {
        "stable": false,              // false ⇒ THE CONTEXT WAS REWRITTEN
        "divergence_index": 3,        // first message index that differs
        "common_prefix": 3,
        "previous_request_id": "...",
        "previous_messages": 41,
        "system_changed": false,
        "tools_changed": false,
        "assistant_echo": "modified"  // "verbatim" | "modified" | "absent" | null
      },
      "status_code": 200,
      "media_type": "text/event-stream",
      "stop_reason": "tool_use",
      "usage": {"input_tokens": 30112, "output_tokens": 214, ...},

      // verbatim level only (and never on `VERBATIM_EXCLUDED_PATHS`):
      "request": { ...the complete request body, exactly as the agent sent it... },
      "response": { ...the complete Anthropic Message, incl. thinking signatures... },
      "response_body": "event: ...",  // only when no structured message exists

      // instead of stop_reason/usage/response when the handler raised:
      "error": "AbridgeError: upstream exploded"
    }

Closing the recorder appends one final line::

    {"schema_version": "abridge.session.v1", "session_id": ..., "turns": N,
     "capture_level": "verbatim", "ts": ...}

A file without that line was truncated (the process died mid-rollout); a
`turn_index` gap marks a row that failed to serialize or write.

## Prefix relation — the compaction signal

The load-bearing field. **When request N+1's message list is not an extension
of request N's, the context was rewritten**, and `prefix.stable` is `false`.
That single fact subsumes compaction detection: a consumer never has to parse
a harness's own boundary records, follow a `parentUuid: null`, or scan a
summary string. It is the Anthropic-face analogue of the token gateway's
`prefix_stable` (which asks the same question of token ids), computed here
over per-message digests because this layer has no tokenizer and must not
invent one.

One API key multiplexes several logical conversations (helper calls,
subagents, reruns), so a single global "previous request" would report a
rewrite on every alternation and the signal would be worthless. Requests are
therefore attributed to a **lane** keyed by the canonicalized system prompt
(`conversation_key`) — the same demux idea `serve.py` already uses for the
token gateway, minus the first user message, because compaction *replaces*
the first user message and keying on it would hide exactly the event we are
trying to surface. Two unrelated conversations that share a system prompt
collide into one lane and read as `stable: false`; a lane evicted past
`max_lanes` reads as a fresh conversation (`previous_request_id: null`).
Both are the safe direction — the computation is deliberately biased so that
a spurious "rewritten" costs a consumer one split, where a spurious "stable"
would splice two unrelated contexts into one training sample.

`assistant_echo` answers the follow-up question — whether the assistant turn
the model produced came back verbatim in the next request's history
(`"verbatim"`), came back altered (`"modified"`, e.g. the harness dropped the
`thinking` blocks), or never came back at all (`"absent"`). `null` when there
is no previous turn to compare against or the prefix already diverged.

## Relationship to the token gateway's `tito.record.v1`

The two records deliberately share an **envelope** and diverge on the
**payload**. Shared field names and semantics: `schema_version`, `session_id`,
`request_id`, `turn_index` (monotonic per file, advancing through failures so
a dropped row leaves a detectable gap), `ts`, `model`, `sampling` (a
whitelist, not a passthrough), a closing session line, strict JSON
(`allow_nan=False`), log-and-serve on write failure, and the meaning of the
prefix fact. One parser reads both streams and one join key (`request_id`)
lines up a row here with a turn there.

They do not share the payload, and forcing them to would be a lie. The token
gateway owns a tokenizer, so it records `prompt_token_ids`,
`completion_logprobs`, and the segment tiling a trainer needs, and asks the
prefix question of token ids. This layer sits in front of a provider it does
not tokenize for, so it records provider-native JSON and asks the prefix
question of message digests. A record here plus the gateway's record for the
same `request_id` is the complete picture; neither one can be rewritten into
the other.

## What this layer cannot see

The tunnel carries no HTTP metadata at all (`proxy.Request` is `path` +
decoded JSON body; `ClientResponse` is bytes + media type + status), so no
`Authorization` header, no `x-api-key`, and no caller credential can reach a
record — not by policy but by construction. Record files are created `0600`.

What is NOT filtered: a secret the user typed into a prompt is conversation
content, and at the verbatim level conversation content is exactly what gets
written. `verbatim` means verbatim; a redaction pass over message bodies
would quietly break the one guarantee this record exists to provide. The
level gate, the file mode, and the operator's choice of record directory are
the controls.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

RECORD_SCHEMA_VERSION = "abridge.record.v1"
SESSION_META_SCHEMA_VERSION = "abridge.session.v1"

# The Anthropic Messages face. `request_facts` / `response_facts` understand
# this body shape and no other, so only rows on this path carry `shape`,
# `sampling`, `conversation_key`, and `prefix`. A `Recorder` wrapping an
# OpenAI-face client still records that client's bodies verbatim; it just
# reports no Anthropic-face derivations for them.
MESSAGES_PATH = "/v1/messages"

# Excluded from verbatim body capture even at the verbatim level: a
# token-count call re-sends a duplicate of the adjacent history and produces
# no completion, so its body roughly doubles the file and reconstructs
# nothing. Those rows say `"capture_level": "metadata"` rather than looking
# like verbatim rows that lost their body.
VERBATIM_EXCLUDED_PATHS = ("/v1/messages/count_tokens",)

# Request-control parameters lifted verbatim into `sampling`. Whitelist, not
# passthrough: the body also carries `system` / `messages` / `tools`, which are
# conversation content and belong behind the verbatim level.
SAMPLING_KEYS = (
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "thinking",
    "tool_choice",
    "service_tier",
)

_DIGEST_CHARS = 16


class CaptureLevel(StrEnum):
    """How much of a call lands on disk. Strictly additive, opt-in upward."""

    OFF = "off"
    METADATA = "metadata"
    VERBATIM = "verbatim"


def canonical_text(value: Any) -> str:
    """Flatten Anthropic content (str or block list) to identity text.

    Text blocks contribute their text — `cache_control` and other decorations
    that do not change what the model reads are ignored — and every other
    block its sorted-JSON form.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, sort_keys=True, ensure_ascii=False, default=repr))
        return "\n".join(parts)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)


def digest(value: Any) -> str:
    """Stable short digest of a JSON-able value (sorted keys, no whitespace).

    Truncated to 64 bits: it only ever compares values inside one session's
    record stream, where a collision would have to hit two messages at the
    same index of one conversation.
    """
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)
    return hashlib.sha256(blob.encode()).hexdigest()[:_DIGEST_CHARS]


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """Everything the `metadata` level knows about one Messages request."""

    model: str | None
    stream: bool
    sampling: dict[str, Any]
    conversation_key: str
    system_digest: str | None
    tools_digest: str | None
    tool_names: tuple[str, ...]
    message_digests: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_digest": self.system_digest,
            "tools_digest": self.tools_digest,
            "tool_names": list(self.tool_names),
            "messages": len(self.message_digests),
            "message_digests": list(self.message_digests),
        }


def request_facts(body: Mapping[str, Any]) -> RequestFacts:
    """Derive the metadata-level view of an Anthropic Messages request body.

    `tools_digest` covers the full schemas, so a consumer can tell "the tool
    set changed" from "the same tools were re-sent" without the schemas
    themselves; `tool_names` is the list the model actually saw on the wire —
    which is not necessarily the list a harness reports about itself.
    """
    system = body.get("system")
    tools = body.get("tools")
    messages = body.get("messages")
    names: list[str] = []
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name")
                if isinstance(name, str):
                    names.append(name)
    return RequestFacts(
        model=body.get("model") if isinstance(body.get("model"), str) else None,
        stream=bool(body.get("stream", False)),
        sampling={key: body[key] for key in SAMPLING_KEYS if body.get(key) is not None},
        conversation_key=digest(canonical_text(system)),
        system_digest=None if system is None else digest(system),
        tools_digest=None if tools is None else digest(tools),
        tool_names=tuple(names),
        message_digests=tuple(digest(m) for m in messages) if isinstance(messages, list) else (),
    )


@dataclass(frozen=True, slots=True)
class ResponseFacts:
    """The metadata-level view of one Anthropic Message the model produced."""

    stop_reason: str | None
    usage: dict[str, Any] | None
    content_blocks: tuple[dict[str, Any], ...] | None
    response_digest: str | None
    assistant_digest: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_blocks": None if self.content_blocks is None else [dict(b) for b in self.content_blocks],
            "response_digest": self.response_digest,
        }


EMPTY_RESPONSE_FACTS = ResponseFacts(
    stop_reason=None, usage=None, content_blocks=None, response_digest=None, assistant_digest=None
)


def response_facts(message: Mapping[str, Any] | None) -> ResponseFacts:
    """Derive the metadata-level view of a structured Anthropic Message.

    `content_blocks` records one entry per emitted block. A `thinking` block
    on providers that return redacted reasoning arrives with empty text and a
    populated opaque `signature`, so both lengths are recorded separately —
    `chars: 0, signature_chars: 210` is a meaningful, checkable shape, and the
    signature itself is preserved verbatim one level up.
    """
    if message is None:
        return EMPTY_RESPONSE_FACTS
    content = message.get("content")
    blocks: list[dict[str, Any]] | None = None
    if isinstance(content, list):
        blocks = [_block_shape(block) for block in content]
    usage = message.get("usage")
    stop_reason = message.get("stop_reason")
    return ResponseFacts(
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        usage=dict(usage) if isinstance(usage, Mapping) else None,
        content_blocks=None if blocks is None else tuple(blocks),
        response_digest=digest(message),
        # Keyed exactly like a history message so it can be compared against
        # the assistant turn the agent echoes back on the next request.
        assistant_digest=digest({"role": "assistant", "content": content}),
    )


def _block_shape(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"type": None, "chars": len(str(block))}
    block_type = block.get("type")
    if block_type == "text":
        return {"type": "text", "chars": len(str(block.get("text", "")))}
    if block_type == "thinking":
        return {
            "type": "thinking",
            "chars": len(str(block.get("thinking", ""))),
            "signature_chars": len(str(block.get("signature", ""))),
        }
    if block_type == "redacted_thinking":
        return {"type": "redacted_thinking", "data_chars": len(str(block.get("data", "")))}
    if block_type == "tool_use":
        name = block.get("name")
        return {
            "type": "tool_use",
            "name": name if isinstance(name, str) else None,
            "input_chars": len(json.dumps(block.get("input") or {}, ensure_ascii=False, default=repr)),
        }
    return {
        "type": block_type if isinstance(block_type, str) else None,
        "chars": len(json.dumps(block, ensure_ascii=False, default=repr)),
    }


@dataclass(frozen=True, slots=True)
class PrefixRelation:
    """How this request's message list relates to the lane's previous one."""

    stable: bool
    divergence_index: int | None
    common_prefix: int
    previous_request_id: str | None
    previous_messages: int | None
    system_changed: bool
    tools_changed: bool
    assistant_echo: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable": self.stable,
            "divergence_index": self.divergence_index,
            "common_prefix": self.common_prefix,
            "previous_request_id": self.previous_request_id,
            "previous_messages": self.previous_messages,
            "system_changed": self.system_changed,
            "tools_changed": self.tools_changed,
            "assistant_echo": self.assistant_echo,
        }


# The first request seen on a lane: nothing to extend, so nothing is broken.
# Mirrors the token gateway's `prefix_stable = not last or ...`.
FIRST_TURN = PrefixRelation(
    stable=True,
    divergence_index=None,
    common_prefix=0,
    previous_request_id=None,
    previous_messages=None,
    system_changed=False,
    tools_changed=False,
    assistant_echo=None,
)


def common_prefix_length(previous: Sequence[str], current: Sequence[str]) -> int:
    """How many leading entries the two digest sequences share."""
    count = 0
    for before, after in zip(previous, current):
        if before != after:
            break
        count += 1
    return count


@dataclass(slots=True)
class _Lane:
    request_id: str
    message_digests: tuple[str, ...]
    system_digest: str | None
    tools_digest: str | None
    assistant_digest: str | None = None


class PrefixTracker:
    """Per-conversation "is this an extension of the last request" bookkeeping.

    `begin` returns the relation for a request and installs it as its lane's
    new baseline; `finish` attaches the assistant turn that request produced,
    so the next `begin` can also report whether that turn was echoed back
    faithfully. Lanes are keyed by `RequestFacts.conversation_key` and
    LRU-bounded at `max_lanes` — an evicted lane's next request reads as a
    fresh conversation, which is why the bound should exceed the number of
    conversations one agent multiplexes (main loop + concurrent subagents).

    Not concurrency-safe in the strict sense: two calls racing on ONE lane
    interleave their baselines. The tracker is used from the recorder's own
    handler coroutine, and the failure direction is `stable: false`, so a race
    costs a spurious split rather than a bad splice.
    """

    def __init__(self, *, max_lanes: int = 16) -> None:
        if max_lanes < 1:
            raise ValueError(f"max_lanes must be >= 1, got {max_lanes!r}")
        self._max_lanes = max_lanes
        self._lanes: OrderedDict[str, _Lane] = OrderedDict()

    def begin(self, *, request_id: str, facts: RequestFacts) -> PrefixRelation:
        key = facts.conversation_key
        lane = self._lanes.get(key)
        relation = FIRST_TURN if lane is None else self._relate(lane, facts)
        self._lanes[key] = _Lane(
            request_id=request_id,
            message_digests=facts.message_digests,
            system_digest=facts.system_digest,
            tools_digest=facts.tools_digest,
        )
        self._lanes.move_to_end(key)
        while len(self._lanes) > self._max_lanes:
            self._lanes.popitem(last=False)
        return relation

    @staticmethod
    def _relate(lane: _Lane, facts: RequestFacts) -> PrefixRelation:
        previous = lane.message_digests
        current = facts.message_digests
        shared = common_prefix_length(previous, current)
        # An extension keeps every previous message, in order, and adds to it.
        # A shorter list that agrees as far as it goes is a truncation, i.e.
        # still a rewrite — `len(current) >= len(previous)` catches that.
        stable = shared == len(previous) and len(current) >= len(previous)
        echo: str | None = None
        if stable and lane.assistant_digest is not None:
            if len(current) > len(previous):
                echo = "verbatim" if current[len(previous)] == lane.assistant_digest else "modified"
            else:
                echo = "absent"
        return PrefixRelation(
            stable=stable,
            divergence_index=None if stable else shared,
            common_prefix=shared,
            previous_request_id=lane.request_id,
            previous_messages=len(previous),
            system_changed=lane.system_digest != facts.system_digest,
            tools_changed=lane.tools_digest != facts.tools_digest,
            assistant_echo=echo,
        )

    def finish(self, *, request_id: str, conversation_key: str, assistant_digest: str | None) -> None:
        """Attach the assistant turn `request_id` produced to its lane.

        Ignored when another call has already taken the lane over (an
        interleaved `begin`), so a race never mislabels somebody else's turn.
        """
        lane = self._lanes.get(conversation_key)
        if lane is not None and lane.request_id == request_id:
            lane.assistant_digest = assistant_digest


__all__ = [
    "EMPTY_RESPONSE_FACTS",
    "FIRST_TURN",
    "MESSAGES_PATH",
    "RECORD_SCHEMA_VERSION",
    "SAMPLING_KEYS",
    "SESSION_META_SCHEMA_VERSION",
    "VERBATIM_EXCLUDED_PATHS",
    "CaptureLevel",
    "PrefixRelation",
    "PrefixTracker",
    "RequestFacts",
    "ResponseFacts",
    "canonical_text",
    "common_prefix_length",
    "digest",
    "request_facts",
    "response_facts",
]
