"""`agentix.bridge.capture` — the derivations behind `abridge.record.v1`.

These are the facts a downstream consumer reads instead of scraping a
harness's own logs: what tools the model actually saw, what the model
actually emitted, and whether the context was rewritten between two calls.
"""

from __future__ import annotations

import pytest
from agentix.bridge.capture import (
    CaptureLevel,
    PrefixTracker,
    canonical_text,
    common_prefix_length,
    digest,
    request_facts,
    response_facts,
)


def _tool(name: str, *, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": f"the {name} tool",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": required or [],
        },
    }


def _body(messages: list[dict], *, system: str = "be brief", tools: list[dict] | None = None) -> dict:
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "temperature": 1.0,
        "system": system,
        "tools": tools if tools is not None else [_tool("Bash"), _tool("Agent")],
        "messages": messages,
    }


# ── request facts ────────────────────────────────────────────────────────


def test_tool_names_come_from_the_wire_body() -> None:
    """The names recorded are the ones in the request the model saw — not
    whatever a harness reports about its own tool surface elsewhere."""
    facts = request_facts(_body([{"role": "user", "content": "hi"}]))
    assert facts.tool_names == ("Bash", "Agent")
    assert facts.model == "claude-sonnet-4-5"
    assert facts.stream is False
    assert facts.sampling == {"max_tokens": 512, "temperature": 1.0}


def test_tools_digest_covers_the_schemas_not_just_the_names() -> None:
    """A tool set can change without any name changing (a parameter becomes
    required, a description is rewritten). `tool_names` alone would call that
    identical; the digest does not."""
    same_names = request_facts(_body([], tools=[_tool("Bash"), _tool("Agent")]))
    changed = request_facts(_body([], tools=[_tool("Bash", required=["command"]), _tool("Agent")]))
    assert same_names.tool_names == changed.tool_names
    assert same_names.tools_digest != changed.tools_digest


def test_conversation_key_ignores_cache_control_decorations() -> None:
    """`cache_control` markers move between turns without changing what the
    model reads, so they must not split a conversation into two lanes."""
    plain = request_facts(_body([], system=[{"type": "text", "text": "be brief"}]))
    cached = request_facts(
        _body([], system=[{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}])
    )
    assert plain.conversation_key == cached.conversation_key
    # The verbatim system prompt still differs, and the row says so.
    assert plain.system_digest != cached.system_digest


def test_absent_system_and_tools_are_null_not_empty() -> None:
    facts = request_facts({"model": "m", "messages": []})
    assert facts.system_digest is None
    assert facts.tools_digest is None
    assert facts.tool_names == ()
    assert facts.message_digests == ()


def test_canonical_text_flattens_blocks_and_digest_is_key_order_stable() -> None:
    assert canonical_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert canonical_text(None) == "" and canonical_text("x") == "x"
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


# ── response facts ───────────────────────────────────────────────────────


def test_thinking_block_shape_separates_text_from_signature() -> None:
    """On this provider a thinking block arrives with empty text and a
    populated opaque signature. `chars: 0, signature_chars: N` is the shape
    that makes that visible at the metadata level."""
    facts = response_facts(
        {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "x" * 210},
                {"type": "redacted_thinking", "data": "y" * 12},
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    )
    assert facts.content_blocks == (
        {"type": "thinking", "chars": 0, "signature_chars": 210},
        {"type": "redacted_thinking", "data_chars": 12},
        {"type": "text", "chars": 5},
        {"type": "tool_use", "name": "Bash", "input_chars": len('{"command": "ls"}')},
    )
    assert facts.stop_reason == "tool_use"
    assert facts.usage == {"input_tokens": 10, "output_tokens": 2}


def test_missing_message_yields_empty_facts() -> None:
    facts = response_facts(None)
    assert facts.content_blocks is None
    assert facts.stop_reason is None and facts.usage is None
    assert facts.response_digest is None and facts.assistant_digest is None


# ── prefix relation ──────────────────────────────────────────────────────


def _turn(tracker: PrefixTracker, request_id: str, messages: list[dict], *, system: str = "be brief"):
    facts = request_facts(_body(messages, system=system))
    return facts, tracker.begin(request_id=request_id, facts=facts)


def test_common_prefix_length() -> None:
    assert common_prefix_length(["a", "b", "c"], ["a", "b", "z"]) == 2
    assert common_prefix_length([], ["a"]) == 0
    assert common_prefix_length(["a"], ["a", "b"]) == 1


def test_first_turn_has_nothing_to_extend() -> None:
    _, relation = _turn(PrefixTracker(), "r1", [{"role": "user", "content": "one"}])
    assert relation.stable is True
    assert relation.divergence_index is None
    assert relation.previous_request_id is None
    assert relation.assistant_echo is None


def test_an_appended_turn_is_stable() -> None:
    tracker = PrefixTracker()
    history = [{"role": "user", "content": "one"}]
    _turn(tracker, "r1", history)
    _, relation = _turn(tracker, "r2", [*history, {"role": "user", "content": "two"}])
    assert relation.stable is True
    assert relation.divergence_index is None
    assert relation.common_prefix == 1
    assert relation.previous_request_id == "r1"
    assert relation.previous_messages == 1


def test_a_rewritten_history_is_not_an_extension() -> None:
    """What compaction looks like from here: same system prompt, a history
    that no longer starts with what the previous request started with. No
    boundary record, no `parentUuid`, no summary string — one boolean."""
    tracker = PrefixTracker()
    _turn(tracker, "r1", [{"role": "user", "content": f"turn {i}"} for i in range(6)])
    _, relation = _turn(tracker, "r2", [{"role": "user", "content": "<summary so far>"},
                                        {"role": "user", "content": "turn 6"}])
    assert relation.stable is False
    assert relation.divergence_index == 0
    assert relation.previous_messages == 6


def test_a_partially_rewritten_history_reports_where_it_diverged() -> None:
    tracker = PrefixTracker()
    head = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    _turn(tracker, "r1", [*head, {"role": "user", "content": "c"}])
    _, relation = _turn(tracker, "r2", [*head, {"role": "user", "content": "REWRITTEN"}])
    assert relation.stable is False
    assert relation.divergence_index == 2
    assert relation.common_prefix == 2


def test_a_truncated_history_is_a_rewrite_too() -> None:
    """Dropping the tail agrees as far as it goes but is not an extension —
    splicing it into one stream would silently lose turns."""
    tracker = PrefixTracker()
    history = [{"role": "user", "content": c} for c in "abc"]
    _turn(tracker, "r1", history)
    _, relation = _turn(tracker, "r2", history[:2])
    assert relation.stable is False
    assert relation.divergence_index == 2


def test_tool_and_system_changes_are_reported_inside_a_lane() -> None:
    tracker = PrefixTracker()
    history = [{"role": "user", "content": "one"}]
    facts = request_facts(_body(history))
    tracker.begin(request_id="r1", facts=facts)
    grown = request_facts(_body([*history, {"role": "user", "content": "two"}], tools=[_tool("Bash")]))
    relation = tracker.begin(request_id="r2", facts=grown)
    assert relation.stable is True
    assert relation.tools_changed is True
    assert relation.system_changed is False


def test_lanes_keep_interleaved_conversations_apart() -> None:
    tracker = PrefixTracker()
    main = [{"role": "user", "content": "main"}]
    _turn(tracker, "r1", main, system="main agent")
    _turn(tracker, "r2", [{"role": "user", "content": "sub"}], system="sub agent")
    _, relation = _turn(tracker, "r3", [*main, {"role": "user", "content": "more"}], system="main agent")
    assert relation.stable is True
    assert relation.previous_request_id == "r1"


def test_lane_eviction_reads_as_a_fresh_conversation() -> None:
    """A bounded tracker forgets the least recent lane. The next request on
    it looks like a first turn — the safe direction, since the alternative is
    comparing against a lane that no longer exists."""
    tracker = PrefixTracker(max_lanes=2)
    _turn(tracker, "r1", [{"role": "user", "content": "a"}], system="one")
    _turn(tracker, "r2", [{"role": "user", "content": "b"}], system="two")
    _turn(tracker, "r3", [{"role": "user", "content": "c"}], system="three")
    _, relation = _turn(tracker, "r4", [{"role": "user", "content": "a"}], system="one")
    assert relation.previous_request_id is None


def test_finish_ignores_a_lane_another_call_already_took_over() -> None:
    """Two calls racing on one lane must not let the loser's response be
    attributed to the winner's baseline."""
    tracker = PrefixTracker()
    history = [{"role": "user", "content": "one"}]
    facts, _ = _turn(tracker, "r1", history)
    _turn(tracker, "r2", [*history, {"role": "user", "content": "two"}])
    tracker.finish(request_id="r1", conversation_key=facts.conversation_key, assistant_digest="stale")

    _, relation = _turn(tracker, "r3", [*history, {"role": "user", "content": "two"},
                                        {"role": "user", "content": "three"}])
    assert relation.assistant_echo is None  # r2 never reported one; r1's is not borrowed


def test_max_lanes_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_lanes"):
        PrefixTracker(max_lanes=0)


# ── levels ───────────────────────────────────────────────────────────────


def test_capture_levels_are_a_named_ladder() -> None:
    assert [level.value for level in CaptureLevel] == ["off", "metadata", "verbatim"]
    assert CaptureLevel("verbatim") is CaptureLevel.VERBATIM
