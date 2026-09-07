"""Re-frame a completed chat completion as an OpenAI-style SSE stream.

The TITO flow forces ``stream=false`` upstream because the token harvest needs
the whole JSON body (``meta_info`` / logprobs). Agents that only speak the
streaming dialect — Pi's ``openai-completions`` provider always sends
``stream: true`` and parses Server-Sent Events, raising "Stream ended without
finish_reason" on anything else — would otherwise break at their first turn.

This module turns the finished completion into the minimal event stream such
a client accepts: one chunk carrying the whole assistant delta (role, content,
reasoning under whatever key the backend used, tool calls with their ``index``),
one terminal chunk with ``finish_reason`` and ``usage``, then ``[DONE]``. The
content is byte-identical to what the JSON body carried; only the framing
changes, so the recorded turn and the served turn stay the same tokens.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import Response

REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")


def completion_to_chunks(completion: dict[str, Any]) -> list[dict[str, Any]]:
    """The chunk objects (already ordered) for one non-streamed completion."""
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("completion has no choices to stream")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("completion choice has no message to stream")

    base: dict[str, Any] = {
        "id": completion.get("id"),
        "object": "chat.completion.chunk",
        "created": completion.get("created"),
        "model": completion.get("model"),
    }
    delta: dict[str, Any] = {"role": message.get("role") or "assistant"}
    content = message.get("content")
    if isinstance(content, str) and content:
        delta["content"] = content
    for key in REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value:
            delta[key] = value
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        delta["tool_calls"] = [
            {"index": index, **call} if isinstance(call, dict) and "index" not in call else call
            for index, call in enumerate(tool_calls)
        ]
    first = {**base, "choices": [{"index": choice.get("index", 0), "delta": delta, "finish_reason": None}]}
    final: dict[str, Any] = {
        **base,
        "choices": [
            {
                "index": choice.get("index", 0),
                "delta": {},
                "finish_reason": choice.get("finish_reason") or "stop",
            }
        ],
    }
    if completion.get("usage") is not None:
        final["usage"] = completion["usage"]
    return [first, final]


def render_sse(chunks: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def build_sse_response(response_body: bytes) -> Response:
    """SSE response for a 200 JSON completion body the harvest already parsed.

    The body was validated by the upstream adapter (choices/message present),
    so a failure here is a programming error, not a client-facing 4xx.
    """
    completion = json.loads(response_body)
    return Response(
        content=render_sse(completion_to_chunks(completion)),
        status_code=200,
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-tito-stream": "reframed"},
    )
