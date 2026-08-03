"""Capture on the Anthropic-native lane — the one a CLI coding agent uses.

`AnthropicClient` forwards Messages verbatim to an Anthropic-protocol
endpoint and the agent asks for streaming, so the wire the agent gets back is
an SSE blob. The client publishes the completed `Message` it already holds,
which is what makes a `Recorder` row carry `thinking` signatures and
`tool_use` inputs as structure instead of a string somebody has to re-parse.
"""

from __future__ import annotations

import json

import pytest
from agentix.bridge import CaptureLevel, Recorder, Request
from agentix.bridge.clients import AnthropicClient

_SIGNATURE = "EqoBCkYIBRgCKkBz" * 12

_CONTENT = [
    {"type": "thinking", "thinking": "", "signature": _SIGNATURE},
    {"type": "text", "text": "listing the tree"},
    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls -la"}},
]

_BODY = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 8192,
    "stream": True,
    "system": [{"type": "text", "text": "You are a coding assistant."}],
    "tools": [
        {
            "name": "Bash",
            "description": "Run a shell command",
            "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
        }
    ],
    "messages": [{"role": "user", "content": "list the repo"}],
}


def _sdk_message():
    from anthropic.types import Message

    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": _CONTENT,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 30112, "output_tokens": 214},
        }
    )


class _FakeStream:
    """The shape `AsyncMessages.stream(...)` returns: an async CM that is also
    an async iterator and can hand back the assembled final message."""

    def __init__(self, message, calls: list[dict]) -> None:
        self._message = message
        self._calls = calls

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return self._message


def _recorded(tmp_path, *, level: CaptureLevel):
    client = AnthropicClient(base_url="http://upstream.invalid", api_key="sk-ant-real", session_id="sess-1")
    calls: list[dict] = []

    def fake_stream(**kwargs):
        calls.append(kwargs)
        return _FakeStream(_sdk_message(), calls)

    client._client.messages.stream = fake_stream  # noqa: SLF001 - stand in for the SDK hop
    recorder = Recorder(client, tmp_path / "run.jsonl", session_id="sess-1", level=level)
    return recorder.abridge_routes()["/v1/messages"], calls


def _rows(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]


@pytest.mark.asyncio
async def test_streaming_native_call_is_recorded_as_structure(tmp_path) -> None:
    """The agent gets `text/event-stream`; the row gets the Message — with
    the opaque `signature` preserved byte-for-byte, since it is the only
    handle on the thinking content."""
    handler, _ = _recorded(tmp_path, level=CaptureLevel.VERBATIM)
    response = await handler(Request(path="/v1/messages", body=_BODY))
    assert response.media_type == "text/event-stream"

    (row,) = _rows(tmp_path)
    assert row["media_type"] == "text/event-stream"
    assert "response_body" not in row
    assert [b["type"] for b in row["response"]["content"]] == ["thinking", "text", "tool_use"]
    assert row["response"]["content"][0]["signature"] == _SIGNATURE
    assert row["response"]["content"][2]["input"] == {"command": "ls -la"}
    assert row["stop_reason"] == "tool_use"
    assert row["usage"]["input_tokens"] == 30112


@pytest.mark.asyncio
async def test_streaming_native_call_records_the_full_request_body(tmp_path) -> None:
    """`system`, the full tool schemas, and the history exactly as sent —
    the ground truth everything downstream is derived from."""
    handler, upstream_calls = _recorded(tmp_path, level=CaptureLevel.VERBATIM)
    await handler(Request(path="/v1/messages", body=_BODY))

    (row,) = _rows(tmp_path)
    assert row["request"] == _BODY
    assert row["request"]["tools"][0]["input_schema"]["properties"] == {"command": {"type": "string"}}
    assert row["shape"]["tool_names"] == ["Bash"]
    # `stream` was popped off the body the SDK saw; the record keeps what the
    # agent actually asked for.
    assert "stream" not in upstream_calls[0]
    assert row["stream"] is True


@pytest.mark.asyncio
async def test_native_client_reuses_the_recorders_request_id(tmp_path) -> None:
    """The join key: the id in the row IS the `x-request-id` this client
    stamps upstream, so a message-level row and a token-level record of the
    same call line up."""
    handler, upstream_calls = _recorded(tmp_path, level=CaptureLevel.METADATA)
    await handler(Request(path="/v1/messages", body=_BODY))

    (row,) = _rows(tmp_path)
    headers = upstream_calls[0]["extra_headers"]
    assert headers["x-request-id"] == row["request_id"]
    assert headers["x-session-id"] == "sess-1"


@pytest.mark.asyncio
async def test_metadata_level_keeps_the_signature_off_disk(tmp_path) -> None:
    handler, _ = _recorded(tmp_path, level=CaptureLevel.METADATA)
    await handler(Request(path="/v1/messages", body=_BODY))

    (row,) = _rows(tmp_path)
    blob = json.dumps(row)
    assert _SIGNATURE not in blob and "ls -la" not in blob and "coding assistant" not in blob
    # The shape still says a thinking block with an empty body and a long
    # signature came back, and which tool the model called.
    assert row["shape"]["content_blocks"][0] == {
        "type": "thinking",
        "chars": 0,
        "signature_chars": len(_SIGNATURE),
    }
    assert row["shape"]["content_blocks"][2]["name"] == "Bash"
