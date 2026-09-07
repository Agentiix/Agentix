"""Golden test: the incremental==from-scratch invariant on the REAL Qwen3.8
tokenizer and its OWN chat template through the `qwen3_5` family.

Qwen3.5 and Qwen3.8 share one template family (Qwen3.8 adds the
`reasoning_effort` system instruction and a `preserve_thinking` switch). The
family differs from Qwen3 in ways the tiny-vocab engine tests cannot see:
the template raises without a user turn, tool results render as a user turn
wrapped in `<tool_response>`, tool calls use the `<function=…><parameter=…>`
XML dialect, and `<think>` / `</think>` are added tokens that must not split.
This module downloads the tokenizer-only files for Qwen/Qwen3.8-27B (a few
MB; the HF cache is reused) and drives a multi-turn tool-calling session
through `LinearTrajectory.prepare_prompt`, asserting the same invariants as
the Qwen3 golden test plus the `reasoning_effort` pin.

Offline behavior: if the tokenizer is neither cached nor downloadable the
module SKIPS (marker: `network`) — it never fails a disconnected run.
"""

from __future__ import annotations

import json

import pytest
from agentix.tito.engine.pretokenize import get_tito_tokenizer
from agentix.tito.engine.trajectory import LinearTrajectory, SessionRecord, SessionRegistry

pytestmark = pytest.mark.network

_REPO = "Qwen/Qwen3.8-27B"


@pytest.fixture(scope="module")
def qwen38_tok():
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(_REPO, local_files_only=True)
    except Exception:
        pass
    try:
        return AutoTokenizer.from_pretrained(_REPO)
    except Exception as exc:  # noqa: BLE001 - hub errors vary by transport
        pytest.skip(f"Qwen3.8 tokenizer unavailable (offline?): {type(exc).__name__}: {exc}")


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


def _simulate_completion(tt, request_messages, assistant_message, prompt_ids, tools):
    """The completion ids a template-canonical model emits: the from-scratch
    render of request+assistant minus the prompt prefix, without the trailing
    newline (the model stops at `<|im_end|>`)."""
    full = tt.render_messages(
        request_messages + [assistant_message], tools=tools, add_generation_prompt=False, tokenize=True
    )
    assert full[: len(prompt_ids)] == prompt_ids, "assistant render must extend the generation prompt"
    completion = full[len(prompt_ids):]
    newline_id = tt.tokenizer.encode("\n", add_special_tokens=False)[0]
    assert completion and completion[-1] == newline_id
    return completion[:-1]


def _conversation():
    system = {"role": "system", "content": "You optimize GPU kernels. Be terse."}
    user1 = {"role": "user", "content": "Make check.py pass, then make benchmark.py faster."}
    assistant1 = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "First look at the task files before editing anything.",
        "tool_calls": [
            {
                "id": "call_0001",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "cat definition.json"})},
            }
        ],
    }
    tool1 = {"role": "tool", "content": '{"op": "rmsnorm", "shape": [4096, 512]}', "tool_call_id": "call_0001"}
    assistant2 = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "RMSNorm over the last dim; run the checker on the skeleton first.",
        "tool_calls": [
            {
                "id": "call_0002",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "python check.py"})},
            },
            {
                "id": "call_0003",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "nvidia-smi -L"})},
            },
        ],
    }
    tool2a = {"role": "tool", "content": "FAILED: 8/8 workloads mismatch", "tool_call_id": "call_0002"}
    tool2b = {"role": "tool", "content": "GPU 0: NVIDIA H100 80GB HBM3", "tool_call_id": "call_0003"}
    assistant3 = {
        "role": "assistant",
        "content": "The skeleton fails all workloads; I will implement the kernel next.",
        "reasoning_content": "Two tool results consumed; summarise and continue.",
    }
    turns = [
        ([system, user1], assistant1),
        ([system, user1, assistant1, tool1], assistant2),
        ([system, user1, assistant1, tool1, assistant2, tool2a, tool2b], assistant3),
    ]
    return turns


@pytest.mark.parametrize("effort", ["xhigh", "low"])
def test_qwen3_8_incremental_equals_from_scratch_multi_turn_tool_calls(qwen38_tok, effort):
    tt = get_tito_tokenizer(
        qwen38_tok, "qwen3_5", allowed_append_roles=("tool",), chat_template_kwargs={"reasoning_effort": effort}
    )
    registry = SessionRegistry(None, qwen38_tok, tito_tokenizer=tt)
    tr = LinearTrajectory()
    turns = _conversation()

    sources: list[list[str]] = []
    for request_messages, assistant in turns:
        prepared = tr.prepare_prompt(request_messages, _TOOLS, tito_tokenizer=tt)
        from_scratch = tt.render_messages(request_messages, tools=_TOOLS, add_generation_prompt=True, tokenize=True)
        assert prepared.token_ids == from_scratch
        assert prepared.prefix_stable is True
        assert prepared.segments[0]["start"] == 0
        assert prepared.segments[-1]["end"] == len(prepared.token_ids)
        for left, right in zip(prepared.segments, prepared.segments[1:], strict=False):
            assert left["end"] == right["start"]
        sources.append([s["source"] for s in prepared.segments])

        completion = _simulate_completion(tt, request_messages, assistant, prepared.token_ids, _TOOLS)
        tr.update_pretokenized_state(
            request_messages,
            assistant,
            prompt_token_ids=prepared.token_ids,
            completion_token_ids=completion,
            max_trim_tokens=tt.max_trim_tokens,
        )
        tr.append_record(SessionRecord(
            timestamp=0.0, method="POST", path="/v1/chat/completions", status_code=200,
            request={"model": "m", "messages": request_messages, "tools": _TOOLS}, response={},
        ))

    assert sources == [
        ["render"],
        ["prefix", "tool", "generation_prompt"],
        ["prefix", "tool", "generation_prompt"],  # two consecutive tool results = one segment
    ]

    final_messages = turns[-1][0] + [turns[-1][1]]
    assert tt.fix_prefix(tr.token_ids) == tt.render_messages(
        final_messages, tools=_TOOLS, add_generation_prompt=False, tokenize=True
    )
    assert registry.compute_session_mismatch(tr) == []

    # The pinned reasoning effort is in the rendered system prompt the model saw.
    rendered = qwen38_tok.decode(tr.token_ids)
    assert f"Reasoning effort is set to {effort}" in rendered
    # Earlier-turn reasoning survives in the accumulated trajectory (preserve_thinking default).
    assert "First look at the task files" in rendered


def test_qwen3_8_generation_prompt_opens_the_think_block(qwen38_tok):
    tt = get_tito_tokenizer(qwen38_tok, "qwen3_5")
    ids = tt.render_messages(
        [{"role": "user", "content": "hi"}], tools=_TOOLS, add_generation_prompt=True, tokenize=True
    )
    tail = qwen38_tok.decode(ids[-6:])
    assert tail.endswith("<|im_start|>assistant\n<think>\n")
    think_id = qwen38_tok.convert_tokens_to_ids("<think>")
    assert think_id in ids[-3:]  # `<think>` is a single added token, never split


def test_default_family_cannot_tokenize_qwen3_8_tool_results(qwen38_tok):
    """Documents WHY the family exists: the model-agnostic engine's synthetic
    context has no user turn and the Qwen3.5/3.8 template rejects it."""
    default = get_tito_tokenizer(qwen38_tok, "default")
    turns = _conversation()
    request1, assistant1 = turns[0]
    with pytest.raises(ValueError):
        default.tokenize_additional_non_assistant(request1 + [assistant1], turns[1][0], _TOOLS)
