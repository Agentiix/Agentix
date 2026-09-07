"""`Recorder` — capture rollout traffic at the tunnel, one JSONL line per call.

The tunnel is the one place every LLM call an agent makes passes through, so
it is the natural recording point for rollout data collection: wrap any
handler client in `Recorder(client, path, level=...)` and hand the wrapper to
`Proxy(...)` — neither the agent nor the upstream can tell the difference.

`capture.py` defines the row shape (`abridge.record.v1`) and the capture
ladder; this module is the sink that assembles, orders, and persists it.
Read that module's docstring for the schema. The short version:

  * `CaptureLevel.METADATA` (the default) writes identity, model, sampling,
    usage, tool names, per-message digests, the response block skeleton, and
    the prefix relation — no conversation text.
  * `CaptureLevel.VERBATIM` adds the complete request body (system, full tool
    schemas, the entire message history) and the complete structured response
    (`thinking` with its opaque `signature`, `text`, `tool_use`).

Full capture is opt-in and a configured level is never promoted.

Identity and joins. `request_id` is minted per call and bound on the
`current_request_id` context var for the duration of the handler, so the
transport layer (`Forward` / the SDK clients) stamps the SAME id as
`x-request-id` on the upstream hop — a downstream token recorder's per-turn
record and this row join on it. `session_id`, when given, identifies the
rollout the wrapped client serves. `gateway_session_id` is read back from the
transport after the call (via `current_upstream_session_id`): when the
downstream is a session-scoped gateway (`SessionForward`), it is the
gateway's OWN session id — i.e. the `session_id` in the gateway's token
records — restoring the session-level join that the caller-side hash alone
cannot provide.

Structure over blobs. An agent that asked for streaming gets `text/event-
stream` bytes back, so the clients publish the completed `Message` dict on
`current_response_message` and the row carries the object. When nothing
published one (a raw pass-through `Forward` relaying somebody else's SSE),
`response` is absent, `response_body` holds the bytes as text, and the
skeleton fields are `null` — the boundary is recorded, never guessed at.

A handler that raises records `{"error": ...}` instead of the response fields
and re-raises — a failed call is signal, not something to lose.

Handlers run on the event loop, so appends never interleave; each line is
flushed as it is written so the file is complete up to the last call even if
the process dies mid-rollout. The file opens lazily (mode `0600`) on the
first record, so a Recorder that never serves (e.g. a route-enumeration
probe) leaves no empty file behind, and `aclose()` appends one
`abridge.session.v1` line so a truncated file is distinguishable from a
closed one. `turn_index` advances even when a line fails, so any dropped row
leaves a detectable gap. Capture is log-and-serve: a failed row write (disk
full, unencodable text) is logged and the agent's call still succeeds —
matching the token-recording gateway's policy, so the two capture layers
never disagree about whether a turn happened. After `aclose()` a straggler
in-flight call's row is dropped (logged), never written to a resurrected file
handle.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import IO, Any

from ._request_id import (
    current_request_id,
    current_response_message,
    current_upstream_session_id,
    mint_request_id,
)
from .capture import (
    MESSAGES_PATH,
    RECORD_SCHEMA_VERSION,
    SESSION_META_SCHEMA_VERSION,
    VERBATIM_EXCLUDED_PATHS,
    CaptureLevel,
    PrefixTracker,
    RequestFacts,
    request_facts,
    response_facts,
)
from .proxy import ClientResponse, Handler, Request, _collect_handlers

logger = logging.getLogger(__name__)


class Recorder:
    """Wrap a handler client; record every (request, response) pair it serves.

    Exposes the inner client's routes via `abridge_routes()` (the blessed
    dynamic-route seam), delegates `environ(...)`, and closes both the inner
    client and the record file on `aclose()` — so `Proxy.stop()` tears the
    whole stack down once, as usual.

    `level` picks how much lands on disk (see `capture.CaptureLevel`); it
    defaults to `METADATA` because verbatim bodies carry whatever the user
    typed. `max_lanes` bounds the prefix tracker's per-conversation state —
    raise it above the number of conversations the agent multiplexes at once
    (its main loop plus concurrent subagents).
    """

    def __init__(
        self,
        client: Any,
        path: str | Path,
        *,
        session_id: str | None = None,
        level: CaptureLevel = CaptureLevel.METADATA,
        max_lanes: int = 16,
    ) -> None:
        if CaptureLevel(level) is CaptureLevel.OFF:
            raise ValueError(
                "Recorder cannot be built at CaptureLevel.OFF — 'off' means no recorder at all; "
                "skip the wrapper instead of installing one that writes nothing"
            )
        self._client = client
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id
        self._level = CaptureLevel(level)
        self._prefix = PrefixTracker(max_lanes=max_lanes)
        self._file: IO[str] | None = None
        self._closed = False
        self._turns = 0

    @property
    def level(self) -> CaptureLevel:
        return self._level

    def abridge_routes(self) -> dict[str, Handler]:
        return {path: self._recording(path, handler) for path, handler in _collect_handlers(self._client).items()}

    def _recording(self, path: str, handler: Handler) -> Handler:
        anthropic_face = path == MESSAGES_PATH
        verbatim = self._level is CaptureLevel.VERBATIM and path not in VERBATIM_EXCLUDED_PATHS

        async def record(request: Request) -> ClientResponse:
            # Reuse an id bound by an even-outer layer; otherwise mint here.
            # Binding it makes the transport's upstream `x-request-id` equal
            # this row's `request_id`.
            request_id = current_request_id.get() or mint_request_id()
            # Derived BEFORE the handler runs: these digests are the ground
            # truth for what the agent sent, and the row is only serialized
            # afterwards. Same reason the verbatim body is deep-copied below.
            facts = request_facts(request.body) if anthropic_face else None
            line = self._open_line(path=path, request_id=request_id, facts=facts, verbatim=verbatim)
            if facts is not None:
                line["prefix"] = self._prefix.begin(request_id=request_id, facts=facts).to_dict()
            if verbatim:
                # Deep-copied because the row is serialized after the handler
                # ran: a handler that rewrote the body in place would
                # otherwise be recorded as what the agent sent.
                line["request"] = copy.deepcopy(request.body)

            rid_token = current_request_id.set(request_id)
            # Cleared per call so a value published by a PREVIOUS call on
            # this task never leaks into an unrelated row.
            upstream_token = current_upstream_session_id.set(None)
            message_token = current_response_message.set(None)
            try:
                response = await handler(request)
            except BaseException as exc:
                line["error"] = f"{type(exc).__name__}: {exc}"
                self._stamp_gateway_session(line)
                self._write(line)
                raise
            finally:
                current_request_id.reset(rid_token)
                gateway_session_id = current_upstream_session_id.get()
                current_upstream_session_id.reset(upstream_token)
                message = current_response_message.get()
                current_response_message.reset(message_token)
            if gateway_session_id is not None:
                line["gateway_session_id"] = gateway_session_id
            self._stamp_response(
                line, response, message, facts=facts, request_id=request_id, verbatim=verbatim
            )
            self._write(line)
            return response

        return record

    def _open_line(
        self, *, path: str, request_id: str, facts: RequestFacts | None, verbatim: bool
    ) -> dict[str, Any]:
        applied = CaptureLevel.VERBATIM if verbatim else CaptureLevel.METADATA
        line: dict[str, Any] = {"schema_version": RECORD_SCHEMA_VERSION}
        if self._session_id is not None:
            line["session_id"] = self._session_id
        line.update(
            {
                "request_id": request_id,
                # Reserved here to fix the key's position; the value is
                # assigned at write time so two concurrent calls can't be
                # handed the same index.
                "turn_index": -1,
                "ts": time.time(),
                "path": path,
                "capture_level": applied.value,
            }
        )
        if facts is not None:
            line.update(
                {
                    "model": facts.model,
                    "stream": facts.stream,
                    "sampling": dict(facts.sampling),
                    "conversation_key": facts.conversation_key,
                    "shape": facts.to_dict(),
                }
            )
        return line

    def _stamp_response(
        self,
        line: dict[str, Any],
        response: ClientResponse,
        message: dict[str, Any] | None,
        *,
        facts: RequestFacts | None,
        request_id: str,
        verbatim: bool,
    ) -> None:
        line["status_code"] = response.status_code
        line["media_type"] = response.media_type
        if message is None and response.media_type == "application/json":
            # No client published structure, but a JSON body on this face IS
            # the Message — decode it rather than treat it as an opaque blob.
            message = _decode_json_object(response)
        if facts is not None:
            derived = response_facts(message)
            self._prefix.finish(
                request_id=request_id,
                conversation_key=facts.conversation_key,
                assistant_digest=derived.assistant_digest,
            )
            shape = line.get("shape")
            if isinstance(shape, dict):
                shape.update(derived.to_dict())
            line["stop_reason"] = derived.stop_reason
            line["usage"] = derived.usage
        if not verbatim:
            return
        if message is not None:
            line["response"] = message
        else:
            # An opaque body (somebody else's SSE relayed by a bare Forward).
            # Recorded as text so nothing is lost, and flagged by the absence
            # of `response` so a consumer never mistakes it for structure.
            line["response_body"] = response.body.decode("utf-8", "replace")

    @staticmethod
    def _stamp_gateway_session(line: dict[str, Any]) -> None:
        gateway_session_id = current_upstream_session_id.get()
        if gateway_session_id is not None:
            line["gateway_session_id"] = gateway_session_id

    def _write(self, line: dict[str, Any]) -> None:
        # Log-and-serve, mirroring the token-recording gateway's policy: the
        # upstream call already succeeded (or its error is being re-raised),
        # so a capture failure must not turn it into a wire error. The
        # turn_index still advances, leaving a detectable gap.
        line["turn_index"] = self._turns
        try:
            self._append(line)
        except Exception:  # noqa: BLE001 - capture must never fail the served call
            logger.exception("abridge recorder: failed to append to %s — row NOT persisted", self._path)
        finally:
            self._turns += 1

    def _append(self, line: dict[str, Any]) -> None:
        if self._closed:
            # A straggler dispatch outlived aclose(): the file is closed for
            # good — dropping the row (loudly) beats resurrecting a file
            # handle nobody will ever close.
            logger.warning("abridge recorder: dropping row for %s — recorder is closed", self._path)
            return
        handle = self._file
        if handle is None or handle.closed:
            # 0600 at creation: a verbatim row holds whatever the user typed,
            # so the file must not be readable by other accounts on the box.
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            handle = os.fdopen(fd, "a", encoding="utf-8")
            self._file = handle
        # Strict JSON: no NaN/Infinity literals, matching the token gateway's
        # record stream, so one parser reads both. `default=repr` covers an
        # exotic value a provider slipped into an otherwise JSON body.
        handle.write(json.dumps(line, ensure_ascii=False, allow_nan=False, default=repr) + "\n")
        handle.flush()

    def environ(self, handle: Any) -> dict[str, str]:
        return self._client.environ(handle)

    async def aclose(self) -> None:
        try:
            aclose = getattr(self._client, "aclose", None)
            if aclose is not None:
                await aclose()
        finally:
            self._finalize()

    def _finalize(self) -> None:
        """Append the session-metadata line and close the file.

        Skipped entirely for a recorder that never wrote a row, so a
        route-enumeration probe still leaves no file behind.
        """
        if self._closed:
            return
        if self._turns:
            meta: dict[str, Any] = {"schema_version": SESSION_META_SCHEMA_VERSION}
            if self._session_id is not None:
                meta["session_id"] = self._session_id
            meta.update({"turns": self._turns, "capture_level": self._level.value, "ts": time.time()})
            try:
                self._append(meta)
            except Exception:  # noqa: BLE001 - a missing trailer is not worth failing teardown
                logger.exception("abridge recorder: failed to finalize %s", self._path)
        self._closed = True
        if self._file is not None:
            self._file.close()


def _decode_json_object(response: ClientResponse) -> dict[str, Any] | None:
    try:
        decoded = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


__all__ = ["Recorder"]
