"""`Recorder` — host-side rollout capture at the tunnel.

Wrapping a client records every call its handlers serve as an
`abridge.record.v1` JSONL row, without the agent or the upstream noticing.
The tunnel is the one place all of an agent's LLM traffic passes, so this is
the natural recording point for rollout data collection.

Two properties carry the weight here: the level ladder (metadata never puts
conversation text on disk; verbatim is the reconstruction ground truth) and
the prefix relation (a row says whether the context was rewritten, so nobody
downstream has to parse a harness's own boundary records).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from agentix.bridge import AbridgeError, CaptureLevel, ClientResponse, Recorder, Request, on
from agentix.bridge._request_id import publish_response_message

MESSAGES = "/v1/messages"
COUNT_TOKENS = "/v1/messages/count_tokens"

_TOOLS = [
    {
        "name": "Bash",
        "description": "Run a shell command",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
    }
]


def _body(*, messages: list[dict], system: str = "be brief", tools: list[dict] | None = None) -> dict:
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "stream": True,
        "system": system,
        "tools": _TOOLS if tools is None else tools,
        "messages": messages,
    }


def _message(content: list[dict], *, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 30112, "output_tokens": 214},
    }


class _EchoClient:
    """An Anthropic-face client that streams (like the real CLI does) and
    publishes the completed Message the way the bundled clients do."""

    def __init__(self, content: list[dict] | None = None) -> None:
        self.closed = False
        self.content = content if content is not None else [{"type": "text", "text": "hi back"}]

    @on(MESSAGES)
    async def messages(self, request: Request) -> ClientResponse:
        message = _message(self.content)
        publish_response_message(message)
        return ClientResponse.sse(b"event: message_stop\ndata: {}\n\n")

    @on(COUNT_TOKENS)
    async def count_tokens(self, request: Request) -> ClientResponse:
        return ClientResponse.json({"input_tokens": 7})

    async def aclose(self) -> None:
        self.closed = True


class _FailingClient:
    @on(MESSAGES)
    async def messages(self, request: Request) -> ClientResponse:
        raise AbridgeError("upstream exploded", status_code=502)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _turns(path: Path) -> list[dict]:
    return [r for r in _rows(path) if r["schema_version"] == "abridge.record.v1"]


async def _call(routes, body: dict, path: str = MESSAGES) -> ClientResponse:
    return await routes[path](Request(path=path, body=body))


# ── levels ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metadata_level_is_the_default_and_writes_no_conversation_text(tmp_path) -> None:
    """Turning capture on must not turn verbatim capture on: the default
    level keeps the join keys, the shape, and the prefix relation, and puts
    no prompt or completion text on disk."""
    out = tmp_path / "run.jsonl"
    recorder = Recorder(_EchoClient(), out)
    assert recorder.level is CaptureLevel.METADATA
    routes = recorder.abridge_routes()
    assert set(routes) == {MESSAGES, COUNT_TOKENS}

    await _call(routes, _body(messages=[{"role": "user", "content": "the secret plan"}]))

    (row,) = _turns(out)
    blob = json.dumps(row)
    assert row["capture_level"] == "metadata"
    assert "request" not in row and "response" not in row and "response_body" not in row
    assert "the secret plan" not in blob and "be brief" not in blob and "hi back" not in blob
    # The parts that are NOT conversation text still land.
    assert row["model"] == "claude-sonnet-4-5"
    assert row["stream"] is True
    assert row["sampling"] == {"max_tokens": 512}
    assert row["shape"]["tool_names"] == ["Bash"]
    assert row["shape"]["messages"] == 1
    assert row["shape"]["content_blocks"] == [{"type": "text", "chars": 7}]
    assert row["stop_reason"] == "end_turn"
    assert row["usage"] == {"input_tokens": 30112, "output_tokens": 214}


@pytest.mark.asyncio
async def test_verbatim_level_records_the_complete_request_body(tmp_path) -> None:
    """The reconstruction ground truth: system, the FULL tool schemas (not
    just their names), and the entire message history exactly as sent."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out, level=CaptureLevel.VERBATIM).abridge_routes()
    body = _body(messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}])

    await _call(routes, body)

    (row,) = _turns(out)
    assert row["capture_level"] == "verbatim"
    assert row["request"] == body
    assert row["request"]["tools"][0]["input_schema"]["properties"] == {"command": {"type": "string"}}


@pytest.mark.asyncio
async def test_verbatim_level_records_thinking_signatures_verbatim(tmp_path) -> None:
    """This provider returns `thinking` blocks with empty text and a
    populated opaque `signature` — the only handle on that content. It has to
    survive byte-for-byte, and the skeleton has to show the shape."""
    out = tmp_path / "run.jsonl"
    content = [
        {"type": "thinking", "thinking": "", "signature": "EqoBCkYIBRgCKkC" * 8},
        {"type": "text", "text": "running it"},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
    ]
    routes = Recorder(_EchoClient(content), out, level=CaptureLevel.VERBATIM).abridge_routes()

    await _call(routes, _body(messages=[{"role": "user", "content": "go"}]))

    (row,) = _turns(out)
    assert row["response"]["content"] == content
    assert row["shape"]["content_blocks"] == [
        {"type": "thinking", "chars": 0, "signature_chars": len(content[0]["signature"])},
        {"type": "text", "chars": 10},
        {"type": "tool_use", "name": "Bash", "input_chars": len(json.dumps({"command": "ls"}))},
    ]


@pytest.mark.asyncio
async def test_streaming_response_is_recorded_as_structure_not_an_sse_blob(tmp_path) -> None:
    """The agent's wire is `text/event-stream`, but the row carries the
    Message object the client published — nobody downstream should have to
    re-parse an SSE string to find a tool_use block."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out, level=CaptureLevel.VERBATIM).abridge_routes()

    await _call(routes, _body(messages=[{"role": "user", "content": "go"}]))

    (row,) = _turns(out)
    assert row["media_type"] == "text/event-stream"
    assert row["response"]["content"] == [{"type": "text", "text": "hi back"}]
    assert "response_body" not in row


@pytest.mark.asyncio
async def test_opaque_body_is_kept_as_text_and_flagged_by_the_missing_structure(tmp_path) -> None:
    """A bare pass-through (nobody published a Message) can only be recorded
    as bytes. That boundary is stated — `response` absent, `content_blocks`
    null — instead of being papered over with a guess."""

    class _Relay:
        @on(MESSAGES)
        async def messages(self, request: Request) -> ClientResponse:
            return ClientResponse.sse(b"event: ping\ndata: {}\n\n")

    out = tmp_path / "run.jsonl"
    routes = Recorder(_Relay(), out, level=CaptureLevel.VERBATIM).abridge_routes()
    await _call(routes, _body(messages=[{"role": "user", "content": "go"}]))

    (row,) = _turns(out)
    assert "response" not in row
    assert row["response_body"] == "event: ping\ndata: {}\n\n"
    assert row["shape"]["content_blocks"] is None
    assert row["stop_reason"] is None


@pytest.mark.asyncio
async def test_count_tokens_stays_at_metadata_even_at_verbatim(tmp_path) -> None:
    """A token-count call re-sends a duplicate of the adjacent history and
    produces no completion. Its body is excluded from verbatim capture, and
    the row says `metadata` rather than looking like a verbatim row that lost
    its body."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out, level=CaptureLevel.VERBATIM).abridge_routes()

    await _call(routes, _body(messages=[{"role": "user", "content": "count me"}]), COUNT_TOKENS)

    (row,) = _turns(out)
    assert row["path"] == COUNT_TOKENS
    assert row["capture_level"] == "metadata"
    assert "request" not in row and "count me" not in json.dumps(row)


@pytest.mark.asyncio
async def test_non_anthropic_paths_keep_their_verbatim_body_without_derivations(tmp_path) -> None:
    """`Recorder` is not Anthropic-only: another face's body is still
    captured verbatim, it just carries no Anthropic-face derivations."""

    class _OpenAIish:
        @on("/v1/chat/completions")
        async def chat(self, request: Request) -> ClientResponse:
            return ClientResponse.json({"choices": [{"message": {"content": "ok"}}]})

    out = tmp_path / "run.jsonl"
    routes = Recorder(_OpenAIish(), out, level=CaptureLevel.VERBATIM).abridge_routes()
    await routes["/v1/chat/completions"](
        Request(path="/v1/chat/completions", body={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    )

    (row,) = _turns(out)
    assert row["request"]["messages"] == [{"role": "user", "content": "hi"}]
    assert row["response"] == {"choices": [{"message": {"content": "ok"}}]}
    assert "shape" not in row and "prefix" not in row


def test_off_level_is_rejected_rather_than_writing_an_empty_file(tmp_path) -> None:
    """`off` means no recorder at all — building one that writes nothing is a
    configuration mistake worth failing loudly on."""
    with pytest.raises(ValueError, match="CaptureLevel.OFF"):
        Recorder(_EchoClient(), tmp_path / "run.jsonl", level=CaptureLevel.OFF)


# ── prefix relation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_appended_turns_are_stable_and_a_rewrite_is_not(tmp_path) -> None:
    """The load-bearing fact. Turn 2 extends turn 1 -> stable. Turn 3
    replaces the history (what compaction does) -> `stable: false` with the
    index where the two diverge, and no boundary record had to be parsed."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out).abridge_routes()

    first = [{"role": "user", "content": "one"}]
    await _call(routes, _body(messages=first))
    grown = [*first, {"role": "assistant", "content": [{"type": "text", "text": "hi back"}]},
             {"role": "user", "content": "two"}]
    await _call(routes, _body(messages=grown))
    compacted = [{"role": "user", "content": "<summary of the conversation so far>"},
                 {"role": "user", "content": "three"}]
    await _call(routes, _body(messages=compacted))

    one, two, three = _turns(out)
    assert one["prefix"]["stable"] is True and one["prefix"]["previous_request_id"] is None
    assert two["prefix"]["stable"] is True
    assert two["prefix"]["previous_request_id"] == one["request_id"]
    assert three["prefix"] == {
        "stable": False,
        "divergence_index": 0,
        "common_prefix": 0,
        "previous_request_id": two["request_id"],
        "previous_messages": 3,
        "system_changed": False,
        "tools_changed": False,
        "assistant_echo": None,
    }


@pytest.mark.asyncio
async def test_assistant_echo_reports_whether_the_model_turn_came_back_intact(tmp_path) -> None:
    """Whether the harness echoed the assistant turn we returned decides
    whether the rows splice into one stream. `verbatim` when it came back
    byte-identical, `modified` when the harness rewrote it."""
    out = tmp_path / "run.jsonl"
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hi back"}]
    routes = Recorder(_EchoClient(content), out).abridge_routes()
    first = [{"role": "user", "content": "one"}]

    # Lane A echoes the assistant turn byte-for-byte; lane B drops the
    # thinking block; lane C re-sends the same history without it at all.
    for system in ("agent A", "agent B", "agent C"):
        await _call(routes, _body(messages=first, system=system))
    await _call(routes, _body(system="agent A", messages=[*first, {"role": "assistant", "content": content},
                                                          {"role": "user", "content": "two"}]))
    stripped = [{"type": "text", "text": "hi back"}]
    await _call(routes, _body(system="agent B", messages=[*first, {"role": "assistant", "content": stripped},
                                                          {"role": "user", "content": "two"}]))
    await _call(routes, _body(system="agent C", messages=first))

    *_, echoed, altered, retried = _turns(out)
    assert [r["prefix"]["stable"] for r in (echoed, altered, retried)] == [True, True, True]
    assert echoed["prefix"]["assistant_echo"] == "verbatim"
    assert altered["prefix"]["assistant_echo"] == "modified"
    assert retried["prefix"]["assistant_echo"] == "absent"


@pytest.mark.asyncio
async def test_interleaved_conversations_do_not_report_false_rewrites(tmp_path) -> None:
    """One key multiplexes several conversations (subagents, helper calls).
    They are tracked in separate lanes keyed by the system prompt, so
    alternating between them is not mistaken for a rewrite."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out).abridge_routes()

    main = [{"role": "user", "content": "main one"}]
    sub = [{"role": "user", "content": "sub one"}]
    await _call(routes, _body(messages=main, system="main agent"))
    await _call(routes, _body(messages=sub, system="sub agent"))
    await _call(routes, _body(messages=[*main, {"role": "user", "content": "main two"}], system="main agent"))

    rows = _turns(out)
    assert [r["prefix"]["stable"] for r in rows] == [True, True, True]
    assert rows[0]["conversation_key"] == rows[2]["conversation_key"] != rows[1]["conversation_key"]
    assert rows[2]["prefix"]["previous_request_id"] == rows[0]["request_id"]


# ── identity, ordering, durability ───────────────────────────────────────


@pytest.mark.asyncio
async def test_rows_carry_session_and_request_ids_and_a_monotonic_turn_index(tmp_path) -> None:
    """Rows are joinable against downstream token records: `session_id` (the
    rollout identity the Recorder was built with) and a per-call
    `request_id`, unique across calls; `turn_index` orders them."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out, session_id="sess-42").abridge_routes()
    for i in range(3):
        await _call(routes, _body(messages=[{"role": "user", "content": f"m{i}"}]))

    rows = _turns(out)
    assert {r["session_id"] for r in rows} == {"sess-42"}
    assert [r["turn_index"] for r in rows] == [0, 1, 2]
    assert len({r["request_id"] for r in rows}) == 3


@pytest.mark.asyncio
async def test_request_id_matches_the_upstream_x_request_id(tmp_path, monkeypatch) -> None:
    """The alignment contract: the id in the row IS the `x-request-id` the
    transport stamps on the upstream hop (via the `current_request_id`
    context var), so a message-level row and a token-level sidecar record for
    the same call join on one key."""
    import httpx
    from agentix.bridge import Forward

    fwd = Forward("http://side.car", paths=[MESSAGES], session_id="sess-1")
    seen_headers: list[dict] = []

    async def fake_post(url, *, json, headers):
        seen_headers.append(dict(headers))
        return httpx.Response(200, content=b'{"ok": true}', headers={"content-type": "application/json"})

    monkeypatch.setattr(fwd._client, "post", fake_post)

    out = tmp_path / "run.jsonl"
    routes = Recorder(fwd, out, session_id="sess-1").abridge_routes()
    await _call(routes, _body(messages=[{"role": "user", "content": "x"}]))

    (row,) = _turns(out)
    assert seen_headers[0]["x-request-id"] == row["request_id"]
    assert seen_headers[0]["x-session-id"] == row["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_handler_errors_are_recorded_and_reraised(tmp_path) -> None:
    out = tmp_path / "run.jsonl"
    routes = Recorder(_FailingClient(), out, session_id="sess-9", level=CaptureLevel.VERBATIM).abridge_routes()
    with pytest.raises(AbridgeError):
        await _call(routes, _body(messages=[{"role": "user", "content": "boom"}]))

    (row,) = _turns(out)
    assert "upstream exploded" in row["error"]
    assert "response" not in row and "status_code" not in row
    # The request side still landed — a failed call is signal, not a hole.
    assert row["request"]["messages"] == [{"role": "user", "content": "boom"}]
    assert row["session_id"] == "sess-9" and row["request_id"]


@pytest.mark.asyncio
async def test_aclose_closes_the_inner_client_and_seals_the_file(tmp_path) -> None:
    """The trailer distinguishes a cleanly closed file from one truncated by
    a process that died mid-rollout."""
    out = tmp_path / "run.jsonl"
    inner = _EchoClient()
    recorder = Recorder(inner, out, session_id="sess-1")
    routes = recorder.abridge_routes()
    await _call(routes, _body(messages=[{"role": "user", "content": "x"}]))
    await recorder.aclose()

    assert inner.closed
    *_, trailer = _rows(out)
    assert trailer == {
        "schema_version": "abridge.session.v1",
        "session_id": "sess-1",
        "turns": 1,
        "capture_level": "metadata",
        "ts": trailer["ts"],
    }


def test_recorder_delegates_environ(tmp_path) -> None:
    class _EnvClient(_EchoClient):
        def environ(self, handle) -> dict[str, str]:
            return {"X": "y"}

    recorder = Recorder(_EnvClient(), tmp_path / "run.jsonl")
    assert recorder.environ(None) == {"X": "y"}


@pytest.mark.asyncio
async def test_recorder_opens_file_lazily(tmp_path) -> None:
    """A Recorder that never serves (e.g. build_session_app's route
    enumeration probe) must leave no empty file behind — not even a trailer."""
    out = tmp_path / "probe.jsonl"
    recorder = Recorder(_EchoClient(), out)
    recorder.abridge_routes()
    await recorder.aclose()
    assert not out.exists()


@pytest.mark.asyncio
async def test_record_file_is_created_private(tmp_path) -> None:
    """A verbatim row holds whatever the user typed; the file must not be
    readable by other accounts on the box."""
    out = tmp_path / "run.jsonl"
    routes = Recorder(_EchoClient(), out, level=CaptureLevel.VERBATIM).abridge_routes()
    await _call(routes, _body(messages=[{"role": "user", "content": "x"}]))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_unserializable_row_is_log_and_serve_and_leaves_an_index_gap(tmp_path, caplog) -> None:
    """Rows are strict JSON — no `NaN`/`Infinity` literals — so one parser
    reads these and the token gateway's records. A row that violates it is
    dropped, not silently emitted as non-standard JSON, and the served call
    still succeeds: `turn_index` advances so the hole is detectable."""
    import logging

    class _NaNOnSecondCall:
        calls = 0

        @on(MESSAGES)
        async def messages(self, request: Request) -> ClientResponse:
            type(self).calls += 1
            message = _message([{"type": "text", "text": "hi"}])
            if type(self).calls == 2:
                message["usage"] = {"input_tokens": float("nan")}
            publish_response_message(message)
            return ClientResponse.json(message)

    out = tmp_path / "run.jsonl"
    routes = Recorder(_NaNOnSecondCall(), out).abridge_routes()

    await _call(routes, _body(messages=[{"role": "user", "content": "one"}]))
    with caplog.at_level(logging.ERROR, logger="agentix.bridge.recorder"):
        response = await _call(routes, _body(messages=[{"role": "user", "content": "two"}]))
    assert response.status_code == 200  # the agent's call still succeeded
    assert any("row NOT persisted" in r.message for r in caplog.records)

    await _call(routes, _body(messages=[{"role": "user", "content": "three"}]))
    assert [r["turn_index"] for r in _turns(out)] == [0, 2]  # index 1 is the detectable hole


@pytest.mark.asyncio
async def test_rows_are_dropped_after_aclose(tmp_path, caplog) -> None:
    """A straggler dispatch that outlives aclose() must not resurrect the
    record file: the row is dropped (logged), the file stays closed, and no
    orphan handle is created."""
    import logging

    out = tmp_path / "run.jsonl"
    recorder = Recorder(_EchoClient(), out)
    routes = recorder.abridge_routes()
    await _call(routes, _body(messages=[{"role": "user", "content": "before"}]))
    await recorder.aclose()

    with caplog.at_level(logging.WARNING, logger="agentix.bridge.recorder"):
        response = await _call(routes, _body(messages=[{"role": "user", "content": "late"}]))
    assert response.status_code == 200  # still served
    assert any("recorder is closed" in r.message for r in caplog.records)
    assert [r["turn_index"] for r in _turns(out)] == [0]  # no late row
    assert recorder._file is not None and recorder._file.closed  # noqa: SLF001 - not reopened
