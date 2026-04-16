"""Anthropic → OpenAI proxy.

Translates Anthropic Messages API to OpenAI Chat Completions,
including tool use / function calling.

Usage:
    export OPENAI_BASE_URL="http://llm-api.model-eval.woa.com/v1"
    export OPENAI_MODEL="api_ali_qwen3-coder-480b-a35b-instruct"
    export OPENAI_API_KEY="..."
    python proxy.py --port 8082

Then set ANTHROPIC_BASE_URL=http://localhost:8082 for Claude Code.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = FastAPI()

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")


def _convert_messages(messages: list[dict]) -> list[dict]:
    """Convert Anthropic messages to OpenAI format."""
    result = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            result.append({"role": role, "content": str(content)})
            continue

        # Process content blocks
        text_parts = []
        tool_calls = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif block.get("type") == "tool_result":
                tool_content = block.get("content", "")
                if isinstance(tool_content, list):
                    tool_content = "\n".join(
                        b.get("text", "") for b in tool_content if b.get("type") == "text"
                    )
                result.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(tool_content),
                })
                continue

        if tool_calls:
            msg_out = {"role": "assistant", "tool_calls": tool_calls}
            if text_parts:
                msg_out["content"] = "\n".join(text_parts)
            else:
                msg_out["content"] = None
            result.append(msg_out)
        elif text_parts:
            result.append({"role": role, "content": "\n".join(text_parts)})

    return result


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    """Convert Anthropic tools to OpenAI format."""
    if not tools:
        return None
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
        },
    } for t in tools]


def _convert_response(openai_resp: dict, model: str) -> dict:
    """Convert OpenAI response to Anthropic format."""
    choice = openai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = openai_resp.get("usage", {})

    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})

    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {"raw": fn.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": fn.get("name", ""),
            "input": args,
        })

    if not content:
        content.append({"type": "text", "text": ""})

    stop_reason = "end_turn"
    finish = choice.get("finish_reason", "")
    if finish in ("tool_calls", "function_call"):
        stop_reason = "tool_use"
    elif finish == "length":
        stop_reason = "max_tokens"

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _stream_response(openai_stream, model: str):
    """Convert OpenAI streaming to Anthropic SSE format."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"

    block_started = False
    tool_blocks: dict[int, dict] = {}  # index -> {id, name, arguments}

    async for line in openai_stream.aiter_lines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        delta = chunk.get("choices", [{}])[0].get("delta", {})

        # Text content
        if delta.get("content"):
            if not block_started:
                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                block_started = True
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta['content']}})}\n\n"

        # Tool calls
        for tc in delta.get("tool_calls", []):
            idx = tc.get("index", 0)
            if idx not in tool_blocks:
                # New tool call
                tool_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}")
                fn_name = tc.get("function", {}).get("name", "")
                tool_blocks[idx] = {"id": tool_id, "name": fn_name, "arguments": ""}
                if block_started:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                    block_started = False
                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': idx + 1, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': fn_name, 'input': {}}})}\n\n"
            args_delta = tc.get("function", {}).get("arguments", "")
            if args_delta:
                tool_blocks[idx]["arguments"] += args_delta
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': idx + 1, 'delta': {'type': 'input_json_delta', 'partial_json': args_delta}})}\n\n"

    # Close open blocks
    if block_started:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
    for idx in tool_blocks:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx + 1})}\n\n"

    stop_reason = "tool_use" if tool_blocks else "end_turn"
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()

    stream = body.get("stream", False)
    system = body.get("system")
    model_requested = body.get("model", "")

    openai_messages = []
    if system:
        if isinstance(system, str):
            openai_messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = " ".join(b.get("text", "") for b in system if b.get("type") == "text")
            openai_messages.append({"role": "system", "content": text})

    openai_messages.extend(_convert_messages(body.get("messages", [])))

    openai_body = {
        "model": OPENAI_MODEL,
        "messages": openai_messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": stream,
    }
    if body.get("temperature") is not None:
        openai_body["temperature"] = body["temperature"]

    openai_tools = _convert_tools(body.get("tools"))
    if openai_tools:
        openai_body["tools"] = openai_tools

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    logger.info("Proxy: %s → %s (stream=%s, msgs=%d, tools=%d)",
                model_requested, OPENAI_MODEL, stream, len(openai_messages),
                len(openai_tools or []))

    try:
        if stream:
            client = httpx.AsyncClient(timeout=300)
            resp = await client.send(
                client.build_request("POST", f"{OPENAI_BASE_URL}/chat/completions",
                                     json=openai_body, headers=headers),
                stream=True,
            )
            if resp.status_code != 200:
                body = await resp.aread()
                await client.aclose()
                return JSONResponse(status_code=resp.status_code, content={"error": body.decode()})

            async def stream_and_close():
                try:
                    async for chunk in _stream_response(resp, model_requested):
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(stream_and_close(), media_type="text/event-stream")
        else:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(f"{OPENAI_BASE_URL}/chat/completions",
                                         json=openai_body, headers=headers)
                if resp.status_code != 200:
                    return JSONResponse(status_code=resp.status_code, content=resp.json())
                return _convert_response(resp.json(), model_requested)
    except Exception as e:
        logger.error("Proxy error: %s", e)
        return JSONResponse(status_code=500, content={"error": {"type": "internal_error", "message": str(e)}})


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    body = await request.json()
    total = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
    return {"input_tokens": total // 4}


@app.get("/")
async def health():
    return {"status": "ok", "backend": OPENAI_BASE_URL, "model": OPENAI_MODEL}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    logger.info("Proxy %s:%d → %s (%s)", args.host, args.port, OPENAI_BASE_URL, OPENAI_MODEL)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
