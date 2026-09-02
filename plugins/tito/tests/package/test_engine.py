"""Self-contained tests for the native TITO engine.

These build a tiny in-memory tokenizer (no model download) and assert the engine's
invariants directly: the incremental tokenization equals a from-scratch render, the
comparator classifies mismatches correctly, message matching collapses falsy
sentinels, and the session state machine rolls back to the last assistant checkpoint.
"""

from __future__ import annotations

import pytest
from agentix.tito.engine.compare import MismatchType, TokenSeqComparator
from agentix.tito.engine.errors import TokenizationError
from agentix.tito.engine.messages import assert_messages_append_only_with_allowed_role, message_matches
from agentix.tito.engine.pretokenize import Qwen3_5TITOTokenizer, Qwen3TITOTokenizer, get_tito_tokenizer
from agentix.tito.engine.trajectory import LinearTrajectory, SessionRegistry
from tokenizers import Tokenizer, models, pre_tokenizers
from transformers import PreTrainedTokenizerFast


@pytest.fixture(scope="module")
def tok():
    specials = ["<unk>", "<s>", "</s>", "<|im_start|>", "<|im_end|>"]
    words = ["system", "user", "assistant", "tool", "dummy", "You", "are", "ok",
             "done", "compute", "17", "23", "391", "X", "Y", "Hello"]
    vocab = {t: i for i, t in enumerate(specials + words)}
    tk = Tokenizer(models.WordLevel(vocab=vocab, unk_token="<unk>"))
    tk.pre_tokenizer = pre_tokenizers.Whitespace()
    t = PreTrainedTokenizerFast(
        tokenizer_object=tk, unk_token="<unk>", bos_token="<s>", eos_token="</s>",
        additional_special_tokens=["<|im_start|>", "<|im_end|>"],
    )
    t.chat_template = (
        "{%- for m in messages -%}<|im_start|>{{ m['role'] }} {{ m['content'] or '' }}<|im_end|>{%- endfor -%}"
        "{%- if add_generation_prompt -%}<|im_start|>assistant {%- endif -%}"
    )
    return t


def _types(ms):
    return [(m.type, m.segment_index) for m in ms]


def test_comparator_classifies_mismatches(tok):
    cmp = TokenSeqComparator(tok, assistant_start_str="<|im_start|>assistant")
    ims, ime = tok.convert_tokens_to_ids("<|im_start|>"), tok.convert_tokens_to_ids("<|im_end|>")
    S, U, A = (tok.convert_tokens_to_ids(w) for w in ("system", "user", "assistant"))
    Y391, Y23, Yok, YH = (tok.convert_tokens_to_ids(w) for w in ("391", "23", "ok", "Hello"))

    assert cmp.compare_sequences([ims, U, Y391, ime], [ims, U, Y391, ime]) == []
    assert _types(cmp.compare_sequences([ims, U, Y391, ime], [ims, U, Y23, ime])) == [
        (MismatchType.NON_ASSISTANT_TEXT, 1)
    ]
    assert _types(cmp.compare_sequences([ims, A, Yok, ime], [ims, A, YH, ime])) == [
        (MismatchType.ASSISTANT_TEXT, 1)
    ]
    assert _types(cmp.compare_sequences([ims, Y391, ime], [ims, Y391, ime, ims])) == [
        (MismatchType.SPECIAL_TOKEN_COUNT, -1)
    ]
    assert _types(cmp.compare_sequences([ims, Y391, ime], [ime, Y391, ims])) == [
        (MismatchType.SPECIAL_TOKEN_TYPE, 0),
        (MismatchType.SPECIAL_TOKEN_TYPE, 2),
    ]
    # trailing trim removes false structural diffs
    assert cmp.compare_sequences([ims, Y391, ime], [ims, Y391, ime, ime], trim_trailing_ids={ime}) == []


def test_message_matches_collapses_falsy_sentinels():
    assert message_matches({"role": "a", "content": ""}, {"role": "a", "content": None})
    assert message_matches({"role": "a", "tool_calls": []}, {"role": "a", "tool_calls": None})
    assert message_matches({"role": "u", "content": "x"}, {"role": "u", "content": "x", "extra": 1})
    # reasoning "\n\n" vs "\n" is non-falsy whitespace → not collapsed (the bug we hit)
    assert not message_matches({"role": "a", "reasoning_content": "\n\n"}, {"role": "a", "reasoning_content": "\n"})
    assert not message_matches({"role": "u", "content": "x"}, {"role": "t", "content": "x"})


def test_render_aliases_reasoning_key_to_reasoning_content():
    """Pi echoes earlier assistant turns with `reasoning` (vLLM's key); Qwen
    templates read `reasoning_content`. Rendering must treat them the same."""
    from agentix.tito.engine.render import normalize_tool_arguments

    via_reasoning = normalize_tool_arguments(
        [{"role": "assistant", "content": "ok", "reasoning": "because"}], "dict"
    )[0]
    assert via_reasoning["reasoning_content"] == "because"
    kept = normalize_tool_arguments(
        [{"role": "assistant", "content": "ok", "reasoning": "x", "reasoning_content": "keep me"}], "dict"
    )[0]
    assert kept["reasoning_content"] == "keep me"
    untouched = normalize_tool_arguments([{"role": "user", "content": "hi", "reasoning": "n/a"}], "dict")[0]
    assert "reasoning_content" not in untouched


def test_message_matches_compares_tool_call_arguments_by_meaning():
    """vLLM stores arguments as json.dumps (": " spacing); Pi echoes them back
    via JSON.stringify (no spaces). Same call, different bytes, must match —
    otherwise the second turn of every tool-using rollout is a false rollback."""
    stored = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "bash", "arguments": "{\"command\": \"ls -la && cat reference.py\"}"},
    }]}
    echoed = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "bash", "arguments": "{\"command\":\"ls -la && cat reference.py\"}"},
    }]}
    assert message_matches(stored, echoed)
    # A genuinely different call is still a mismatch.
    other = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "bash", "arguments": "{\"command\":\"rm -rf /\"}"},
    }]}
    assert not message_matches(stored, other)
    # Different id or name also mismatch; unparseable strings compare verbatim.
    assert not message_matches(stored, {**echoed, "tool_calls": [{**echoed["tool_calls"][0], "id": "c2"}]})
    def raw(arguments):
        call = {"id": "c1", "function": {"name": "f", "arguments": arguments}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}

    raw_a, raw_b = raw("not json"), raw("not  json")
    assert not message_matches(raw_a, raw_b)


def test_message_matches_reasoning_tolerates_absence_and_key_dialect():
    """Reasoning is model-generated and routinely dropped on echo
    (openai-python keeps only the standard fields), and lives under
    `reasoning_content` (sglang/DeepSeek) or `reasoning` (vLLM) — absence on
    either side matches; two present-but-different values do not."""
    stored = {"role": "assistant", "content": "x", "reasoning": "why"}
    assert message_matches(stored, {"role": "assistant", "content": "x"})
    assert message_matches(stored, {"role": "assistant", "content": "x", "reasoning": "why"})
    assert message_matches(stored, {"role": "assistant", "content": "x", "reasoning_content": "why"})
    assert not message_matches(stored, {"role": "assistant", "content": "x", "reasoning_content": "other"})
    assert message_matches({"role": "assistant", "content": "x", "reasoning_content": "why"}, stored)


def test_append_only_enforced():
    stored = [{"role": "user", "content": "x"}]
    assert_messages_append_only_with_allowed_role(stored, stored + [{"role": "tool", "content": "y"}], ["tool"])
    with pytest.raises(ValueError):
        assert_messages_append_only_with_allowed_role(stored, stored + [{"role": "user", "content": "z"}], ["tool"])
    with pytest.raises(ValueError):
        assert_messages_append_only_with_allowed_role(stored, [{"role": "user", "content": "DIFF"}], ["tool"])


@pytest.mark.parametrize(
    "appends",
    [
        [{"role": "tool", "content": "391"}],
        [{"role": "tool", "content": "391"}, {"role": "tool", "content": "23"}],
        [{"role": "user", "content": "Hello"}],
        [{"role": "tool", "content": "X"}, {"role": "user", "content": "Y"}],
    ],
)
def test_incremental_equals_full_render(tok, appends):
    """The core invariant: merge(prefix, incremental) == full from-scratch render."""
    tt = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool", "user"))
    old = [{"role": "system", "content": "You are"}, {"role": "user", "content": "compute 17 23"},
           {"role": "assistant", "content": "ok"}]
    new = old + appends
    prefix = tt.render_messages(old, add_generation_prompt=False, tokenize=True)
    merged = tt.merge_tokens(old, new, prefix, None)
    full = tt.render_messages(new, add_generation_prompt=True, tokenize=True)
    assert merged == full


def test_qwen3_newline_fixup():
    class FakeTok:
        def encode(self, t, add_special_tokens=False):
            return [99]  # "\n" -> single id

        def convert_tokens_to_ids(self, t):
            return 88  # "<|im_end|>"

    q = Qwen3TITOTokenizer(FakeTok(), chat_template_kwargs={"chat_template": "x"})
    q.tokenize_additional_non_assistant = lambda o, n, t=None: [1, 2, 3]
    assert q.merge_tokens([], [], [7, 88], None) == [7, 88, 99, 1, 2, 3]  # prefix ends in im_end -> insert \n
    assert q.merge_tokens([], [], [7, 5], None) == [7, 5, 1, 2, 3]  # otherwise no insert


def test_trajectory_rollback_to_assistant_checkpoint(tok):
    tt = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool", "user"))
    reg = SessionRegistry(None, tok, tito_tokenizer=tt)
    tr = LinearTrajectory()
    sys = [{"role": "system", "content": "You are"}, {"role": "user", "content": "compute 17 23"}]
    a0 = {"role": "assistant", "content": "ok"}

    tr.prepare_pretokenized(sys, None, tito_tokenizer=tt)
    tr.update_pretokenized_state(
        sys, a0, tt.render_messages(sys + [a0], add_generation_prompt=False, tokenize=True), [], tt.max_trim_tokens
    )

    m1 = sys + [a0, {"role": "tool", "content": "391"}]
    a1 = {"role": "assistant", "content": "done"}
    tr.prepare_pretokenized(m1, None, tito_tokenizer=tt)
    tr.update_pretokenized_state(
        m1, a1, tt.render_messages(m1 + [a1], add_generation_prompt=False, tokenize=True), [], tt.max_trim_tokens
    )
    assert tr.num_assistant == 2
    assert reg.compute_session_mismatch(tr) == []  # clean chain → no mismatch

    # retry the tool turn with a different result → rollback to a0 checkpoint
    tr.prepare_pretokenized(sys + [a0, {"role": "tool", "content": "X"}], None, tito_tokenizer=tt)
    assert tr.num_assistant == 1
    assert [m.get("role") for m in tr.messages] == ["system", "user", "assistant"]


def test_trajectory_keeps_only_reachable_checkpoints(tok):
    """Single-step rollback (MAX_ASSISTANT_ROLLBACK_STEPS=1) can only ever reach
    the last two checkpoints; retaining every full prefix+completion token list
    is O(turns^2) dead memory per session. Rollback must still work."""
    tt = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool", "user"))
    tr = LinearTrajectory()
    msgs = [{"role": "system", "content": "You are"}, {"role": "user", "content": "compute 17 23"}]

    for i, reply in enumerate(["ok", "done", "ok", "done"]):
        tr.prepare_pretokenized(msgs, None, tito_tokenizer=tt)
        asst = {"role": "assistant", "content": reply}
        tr.update_pretokenized_state(
            msgs, asst,
            tt.render_messages(msgs + [asst], add_generation_prompt=False, tokenize=True),
            [], tt.max_trim_tokens,
        )
        msgs = msgs + [asst, {"role": "tool", "content": "391"}]

    assert tr.num_assistant == 4
    assert len(tr.trajectory_token_ids) <= 2  # dead checkpoints dropped

    # single-step retry (divergent tool result → rollback one assistant)
    # still works across the trimmed history
    retry = msgs[:-3] + [{"role": "tool", "content": "X"}]
    tr.prepare_pretokenized(retry, None, tito_tokenizer=tt)
    assert tr.num_assistant == 3
    assert tr.token_ids  # the surviving checkpoint is intact


def test_prefix_mismatch_diagnostic_handles_shorter_new_sequence(tok):
    """A new prompt+completion SHORTER than the stored checkpoint must produce
    the TokenizationError diagnostic, not a bare ValueError from zip(strict)."""
    tr = LinearTrajectory()
    tr.trajectory_token_ids.append(list(range(10)))
    tr.num_assistant = 1
    with pytest.raises(TokenizationError):
        # prefix-consistent but SHORTER than the stored checkpoint: the
        # diagnostic scan finds no differing pair before the short side ends.
        tr.update_pretokenized_state(
            [{"role": "user", "content": "compute"}],
            {"role": "assistant", "content": "ok"},
            prompt_token_ids=[0],
            completion_token_ids=[1],
            max_trim_tokens=0,
        )


def test_rollback_survives_exhausted_checkpoints(tok):
    """Retry chains can outrun the trimmed checkpoint window: rollback with no
    intervening update (failed proxy), then another legal rollback. The prompt
    must then be the full from-scratch render — never a merge onto an empty
    prefix that silently drops the whole stored history."""
    tt = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool", "user"))
    tr = LinearTrajectory()
    msgs = [{"role": "system", "content": "You are"}, {"role": "user", "content": "compute 17 23"}]
    for reply in ("ok", "done", "ok"):
        tr.prepare_pretokenized(msgs, None, tito_tokenizer=tt)
        asst = {"role": "assistant", "content": reply}
        tr.update_pretokenized_state(
            msgs, asst,
            tt.render_messages(msgs + [asst], add_generation_prompt=False, tokenize=True),
            [], tt.max_trim_tokens,
        )
        msgs = msgs + [asst, {"role": "tool", "content": "391"}]

    assert tr.num_assistant == 3

    # retry turn 3 (divergent tool after a1) — proxy fails, so no update lands
    stored = tr.messages
    retry3 = stored[:5] + [{"role": "tool", "content": "X"}]
    tr.prepare_pretokenized(retry3, None, tito_tokenizer=tt)
    assert tr.num_assistant == 2

    # retry turn 2 (divergent tool after a0) — checkpoint window exhausted
    retry2 = tr.messages[:3] + [{"role": "tool", "content": "Y"}]
    prompt = tr.prepare_pretokenized(retry2, None, tito_tokenizer=tt)
    assert tr.num_assistant == 1
    assert prompt == tt.render_messages(retry2, add_generation_prompt=True, tokenize=True)


def test_rejected_divergent_request_does_not_commit_rollback(tok):
    """A divergent request that FAILS validation (disallowed appended role)
    must be a pure 400: no rollback side effects may survive, and the original
    branch must remain resumable."""
    from agentix.tito.engine.errors import MessageValidationError

    tt = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool",))
    tr = LinearTrajectory()
    sys = [{"role": "system", "content": "You are"}, {"role": "user", "content": "compute 17 23"}]
    a0 = {"role": "assistant", "content": "ok"}
    tr.prepare_pretokenized(sys, None, tito_tokenizer=tt)
    tr.update_pretokenized_state(
        sys, a0, tt.render_messages(sys + [a0], add_generation_prompt=False, tokenize=True), [], tt.max_trim_tokens
    )
    m1 = sys + [a0, {"role": "tool", "content": "391"}]
    a1 = {"role": "assistant", "content": "done"}
    tr.prepare_pretokenized(m1, None, tito_tokenizer=tt)
    tr.update_pretokenized_state(
        m1, a1, tt.render_messages(m1 + [a1], add_generation_prompt=False, tokenize=True), [], tt.max_trim_tokens
    )
    assert tr.num_assistant == 2

    # divergent at the tool turn AND appends a disallowed user message
    bad = sys + [a0, {"role": "tool", "content": "X"}, {"role": "user", "content": "Hello"}]
    with pytest.raises(MessageValidationError):
        tr.prepare_pretokenized(bad, None, tito_tokenizer=tt)
    assert tr.num_assistant == 2  # rollback was NOT committed
    assert len(tr.records) == 0 or tr.num_assistant == 2  # state intact

    # the original branch is still resumable
    m2 = m1 + [a1, {"role": "tool", "content": "23"}]
    tr.prepare_pretokenized(m2, None, tito_tokenizer=tt)
    assert tr.num_assistant == 2


def test_version_advances_on_rollback_and_update(tok):
    """`num_assistant` is not monotonic (rollback decrements, update increments)
    so it cannot detect concurrent interference; `version` must advance on BOTH."""
    tt = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool", "user"))
    tr = LinearTrajectory()
    sys = [{"role": "system", "content": "You are"}, {"role": "user", "content": "compute 17 23"}]
    a0 = {"role": "assistant", "content": "ok"}
    tr.prepare_pretokenized(sys, None, tito_tokenizer=tt)
    tr.update_pretokenized_state(
        sys, a0, tt.render_messages(sys + [a0], add_generation_prompt=False, tokenize=True), [], tt.max_trim_tokens
    )
    m1 = sys + [a0, {"role": "tool", "content": "391"}]
    a1 = {"role": "assistant", "content": "done"}
    tr.prepare_pretokenized(m1, None, tito_tokenizer=tt)
    tr.update_pretokenized_state(
        m1, a1, tt.render_messages(m1 + [a1], add_generation_prompt=False, tokenize=True), [], tt.max_trim_tokens
    )

    v0 = tr.version
    n0 = tr.num_assistant
    # rollback (retry the tool turn) then a fresh update: num_assistant is
    # back to n0 but version must have moved.
    retry = sys + [a0, {"role": "tool", "content": "X"}]
    tr.prepare_pretokenized(retry, None, tito_tokenizer=tt)
    a1b = {"role": "assistant", "content": "done"}
    tr.update_pretokenized_state(
        retry, a1b,
        tt.render_messages(retry + [a1b], add_generation_prompt=False, tokenize=True),
        [], tt.max_trim_tokens,
    )
    assert tr.num_assistant == n0
    assert tr.version != v0


# A Qwen3.5-shaped template on the tiny vocab: raises without a real user
# turn, wraps tool results in a user turn, and writes `<|im_end|>` + newline
# after every message — the properties the qwen3_5 family exists for.
_QWEN35_LIKE_TEMPLATE = (
    "{%- set ns = namespace(found=false) -%}"
    "{%- for m in messages -%}{%- if m['role'] == 'user' -%}{%- set ns.found = true -%}{%- endif -%}{%- endfor -%}"
    "{%- if not ns.found -%}{{ raise_exception('No user query found in messages.') }}{%- endif -%}"
    "{%- for m in messages -%}"
    "{%- if m['role'] == 'system' and not loop.first -%}"
    "{{ raise_exception('System message must be at the beginning.') }}{%- endif -%}"
    "{%- if m['role'] == 'tool' -%}<|im_start|>user {{ m['content'] or '' }}<|im_end|>{{ '\\n' }}"
    "{%- else -%}<|im_start|>{{ m['role'] }} {{ m['content'] or '' }}<|im_end|>{{ '\\n' }}{%- endif -%}"
    "{%- endfor -%}"
    "{%- if add_generation_prompt -%}<|im_start|>assistant {%- endif -%}"
)


@pytest.fixture(scope="module")
def qwen35_like_tok():
    specials = ["<unk>", "<s>", "</s>", "<|im_start|>", "<|im_end|>"]
    words = ["system", "user", "assistant", "tool", "dummy", "You", "are", "ok",
             "done", "compute", "17", "23", "391", "X", "Y", "Hello", "\n"]
    vocab = {t: i for i, t in enumerate(specials + words)}
    tk = Tokenizer(models.WordLevel(vocab=vocab, unk_token="<unk>"))
    tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    t = PreTrainedTokenizerFast(
        tokenizer_object=tk, unk_token="<unk>", bos_token="<s>", eos_token="</s>",
        additional_special_tokens=["<|im_start|>", "<|im_end|>"],
    )
    # The Qwen families need "\n" to be one token (the `<|im_end|>` newline fixup).
    t.add_tokens(["\n"])
    t.chat_template = _QWEN35_LIKE_TEMPLATE
    return t


def test_qwen3_5_family_renders_synthetic_contexts_with_a_user_turn(qwen35_like_tok):
    tok = qwen35_like_tok
    # The `default` engine's synthetic base (dummy system only) is rejected by
    # this template family ...
    default = get_tito_tokenizer(tok, "default", allowed_append_roles=("tool",))
    with pytest.raises(ValueError):
        default.tokenize_additional_non_assistant(
            [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "ok"}],
            [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "ok"},
             {"role": "tool", "content": "17", "tool_call_id": "c1"}],
        )
    # ... while qwen3_5 carries a dummy user turn and the incremental result
    # equals the from-scratch render (the engine invariant).
    tt = get_tito_tokenizer(tok, "qwen3_5", allowed_append_roles=("tool",))
    assert isinstance(tt, Qwen3_5TITOTokenizer)
    assert tt.tool_call_parser == "qwen3_coder"
    old = [{"role": "system", "content": "You are"}, {"role": "user", "content": "Hello"},
           {"role": "assistant", "content": "ok"}]
    new = old + [{"role": "tool", "content": "17", "tool_call_id": "c1"}]
    prefix = tt.render_messages(old, add_generation_prompt=False, tokenize=True)
    merged = tt.merge_tokens(old, new, prefix)
    assert merged == tt.render_messages(new, add_generation_prompt=True, tokenize=True)


def test_qwen3_5_family_rejects_mid_conversation_system_append(qwen35_like_tok):
    tt = get_tito_tokenizer(qwen35_like_tok, "qwen3_5", allowed_append_roles=("tool", "system"))
    old = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "ok"}]
    with pytest.raises(ValueError, match="position 0"):
        tt.tokenize_additional_non_assistant(old, old + [{"role": "system", "content": "X"}])


def test_chat_template_kwargs_are_pinned_into_every_render(qwen35_like_tok):
    tok = qwen35_like_tok
    tok.chat_template = "{{ effort }}:" + _QWEN35_LIKE_TEMPLATE
    try:
        tt = get_tito_tokenizer(tok, "qwen3_5", chat_template_kwargs={"effort": "X"})
        text = tt.render_messages([{"role": "user", "content": "Hello"}], add_generation_prompt=True)
        assert text.startswith("X:")
        with pytest.raises(ValueError, match="chat_template"):
            get_tito_tokenizer(tok, "qwen3_5", chat_template_kwargs={"chat_template": "y"})
    finally:
        tok.chat_template = _QWEN35_LIKE_TEMPLATE
