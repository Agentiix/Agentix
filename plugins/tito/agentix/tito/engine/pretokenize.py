"""TITO tokenizer — incremental tokenization for pretokenized-prefix reuse.

The base `TITOTokenizer` holds the whole model-agnostic algorithm: it computes the
token IDs for non-assistant messages (tool/user/system) appended after the
assistant's generated tokens, by rendering each segment in a minimal synthetic
context and taking the suffix, then merges them onto the stored prefix. A model
subclass only fixes boundary tokens at the junction (e.g. Qwen3's missing newline
after `<|im_end|>`) and points at its fixed chat template.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .compare import TokenSeqComparator
from .messages import assert_messages_append_only_with_allowed_role
from .render import apply_chat_template

TEMPLATE_DIR = Path(__file__).parent / "templates"
_VALID_ROLES = frozenset({"tool", "user", "system"})
_DUMMY_SYSTEM: dict[str, Any] = {"role": "system", "content": "dummy system"}
_DUMMY_USER: dict[str, Any] = {"role": "user", "content": "dummy user"}


def _build_dummy_assistant(tool_responses: list[dict[str, Any]]) -> dict[str, Any]:
    """A dummy assistant whose tool_calls match *tool_responses*, so the template
    renders the following tool-response turn boundaries correctly."""
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": " ",
        "tool_calls": [
            {
                "id": resp.get("tool_call_id") or f"call0000{i}",
                "type": "function",
                "function": {"name": resp.get("name") or "dummy_func", "arguments": {}},
            }
            for i, resp in enumerate(tool_responses)
        ],
    }


class TITOTokenizer:
    """Incremental tokenization + prefix merging for appended non-assistant turns."""

    max_trim_tokens: int = 0
    trailing_token_ids: frozenset[int] = frozenset()
    reasoning_parser: str | None = None
    tool_call_parser: str | None = None

    def __init__(
        self,
        tokenizer: Any,
        chat_template_kwargs: dict[str, Any] | None = None,
        assistant_start_str: str | None = None,
        special_token_ids: set[int] | None = None,
        allowed_append_roles: list[str] | None = None,
    ) -> None:
        self.tokenizer = tokenizer
        self.chat_template_kwargs = chat_template_kwargs or {}
        self._assistant_start_str = assistant_start_str
        self.allowed_append_roles: list[str] = allowed_append_roles if allowed_append_roles is not None else ["tool"]
        self.special_token_ids = special_token_ids

    def create_comparator(self) -> TokenSeqComparator:
        return TokenSeqComparator(
            self.tokenizer,
            assistant_start_str=self._assistant_start_str,
            special_token_ids=self.special_token_ids,
            trim_trailing_ids=self.trailing_token_ids or None,
        )

    def render_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        add_generation_prompt: bool,
        tools: list[dict[str, Any]] | None = None,
        tokenize: bool = False,
    ) -> Any:
        return apply_chat_template(
            messages,
            tokenizer=self.tokenizer,
            tokenize=tokenize,
            add_generation_prompt=add_generation_prompt,
            tools=tools,
            **self.chat_template_kwargs,
        )

    def _encode_text(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False)

    def _split_appended_segments(self, appended_messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        segments: list[list[dict[str, Any]]] = []
        i = 0
        while i < len(appended_messages):
            role = appended_messages[i]["role"]
            if role == "tool":
                j = i + 1
                while j < len(appended_messages) and appended_messages[j]["role"] == "tool":
                    j += 1
                segments.append(appended_messages[i:j])
                i = j
                continue
            if role in {"user", "system"}:
                segments.append([appended_messages[i]])
                i += 1
                continue
            raise ValueError(f"unsupported appended role for TITO segmentation: {role}")
        return segments

    def _tokenize_rendered_suffix(
        self,
        base_messages: list[dict[str, Any]],
        appended_messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        add_generation_prompt: bool = False,
    ) -> list[int]:
        text_without = self.render_messages(base_messages, add_generation_prompt=False, tools=tools)
        text_with = self.render_messages(
            base_messages + appended_messages, add_generation_prompt=add_generation_prompt, tools=tools
        )
        if not text_with.startswith(text_without):
            roles = [m["role"] for m in appended_messages] if appended_messages else ["generation_prompt"]
            raise ValueError(f"rendered suffix diff failed for {roles}")
        return self._encode_text(text_with[len(text_without):])

    def _tokenize_tool_segment(
        self, appended_messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> list[int]:
        return self._tokenize_rendered_suffix(
            [_DUMMY_SYSTEM, _build_dummy_assistant(appended_messages)], appended_messages, tools=tools
        )

    def _tokenize_user_and_system_segment(
        self, appended_message: dict[str, Any], tools: list[dict[str, Any]] | None = None
    ) -> list[int]:
        return self._tokenize_rendered_suffix([_DUMMY_SYSTEM], [appended_message], tools=tools)

    def appended_segments(
        self,
        old_messages: list[dict[str, Any]],
        new_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, list[int]]]:
        """Per-segment incremental token IDs for the non-assistant messages
        appended after the pretokenized prefix, as ``(source, token_ids)``
        pairs — one per appended role segment plus a final
        ``("generation_prompt", ...)`` entry. The flat concatenation equals
        `tokenize_additional_non_assistant`; the segment boundaries feed the
        per-turn record's ``prompt_segments``."""
        assert_messages_append_only_with_allowed_role(old_messages, new_messages, self.allowed_append_roles)
        appended_messages = new_messages[len(old_messages):]
        segments: list[tuple[str, list[int]]] = []
        for segment in self._split_appended_segments(appended_messages):
            role = segment[0]["role"]
            if role == "tool":
                segments.append((role, self._tokenize_tool_segment(segment, tools)))
            elif role in ("user", "system"):
                segments.append((role, self._tokenize_user_and_system_segment(segment[0], tools)))
            else:
                raise ValueError(f"unsupported appended role for TITO tokenization: {role}")
        segments.append(
            (
                "generation_prompt",
                self._tokenize_rendered_suffix(new_messages, [], tools=tools, add_generation_prompt=True),
            )
        )
        return segments

    def tokenize_additional_non_assistant(
        self,
        old_messages: list[dict[str, Any]],
        new_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> list[int]:
        """Incremental token IDs (incl. the next generation prompt) for the
        non-assistant messages appended after the pretokenized prefix."""
        return [tid for _, ids in self.appended_segments(old_messages, new_messages, tools) for tid in ids]

    def fix_prefix(self, pretokenized_token_ids: list[int]) -> list[int]:
        """Canonicalize a stored prefix before merging (returns a new list).
        The base engine is a no-op; a model subclass re-inserts boundary
        tokens the model omits when it stops (see `Qwen3TITOTokenizer`)."""
        return list(pretokenized_token_ids)

    def merge_tokens(
        self,
        old_messages: list[dict[str, Any]],
        new_messages: list[dict[str, Any]],
        pretokenized_token_ids: list[int],
        tools: list[dict[str, Any]] | None = None,
    ) -> list[int]:
        """Default: concatenate the (canonicalized) stored prefix with the
        incremental tokens."""
        incremental = self.tokenize_additional_non_assistant(old_messages, new_messages, tools)
        return self.fix_prefix(pretokenized_token_ids) + incremental


class Qwen3TITOTokenizer(TITOTokenizer):
    """Qwen3: the model stops at `<|im_end|>` without the trailing `\\n` the template
    emits, so `fix_prefix` re-inserts it so the stored prefix stays canonical."""

    reasoning_parser = "qwen3"
    tool_call_parser = "qwen25"
    _default_assistant_start_str = "<|im_start|>assistant"

    def __init__(
        self,
        tokenizer: Any,
        chat_template_kwargs: dict[str, Any] | None = None,
        assistant_start_str: str | None = None,
        allowed_append_roles: list[str] | None = None,
    ) -> None:
        super().__init__(
            tokenizer,
            chat_template_kwargs,
            assistant_start_str or self._default_assistant_start_str,
            allowed_append_roles=allowed_append_roles,
        )
        nl_ids = tokenizer.encode("\n", add_special_tokens=False)
        if len(nl_ids) != 1:
            raise ValueError(f"expected a single newline token, got {nl_ids}")
        self._newline_id: int = nl_ids[0]
        self._im_end_id: int = tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.trailing_token_ids = frozenset({self._newline_id})

    def fix_prefix(self, pretokenized_token_ids: list[int]) -> list[int]:
        prefix = list(pretokenized_token_ids)
        if prefix and prefix[-1] == self._im_end_id:
            prefix.append(self._newline_id)
        return prefix


class Qwen3_5TITOTokenizer(Qwen3TITOTokenizer):
    """Qwen3.5 / Qwen3.8 (`Qwen3_5ForConditionalGeneration`): the tokenizer's OWN
    chat template, not a bundled fixed one.

    Two properties of that template family shape this subclass (verified on the
    real Qwen/Qwen3.8-27B tokenizer, 2026-09-02):

    - it raises ``No user query found in messages`` when the conversation has
      no real user turn, so the synthetic contexts the incremental algorithm
      renders (`dummy system` + `dummy assistant`) must also carry a dummy
      user message — the suffix diff is unaffected because the dummy turns are
      in both renders;
    - an appended ``system`` message raises ``System message must be at the
      beginning``, so only ``tool`` (and, for Qwen3.8 whose template defaults
      to ``preserve_thinking``, ``user``) appends are supported.

    Reasoning is rendered from ``reasoning_content`` only (the `</think>`
    content-splitting fallback of Qwen3 is gone), tool calls use the
    ``<function=…><parameter=…>`` XML dialect (vLLM parser ``qwen3_coder``),
    and the model stops at ``<|im_end|>`` without the template's trailing
    newline exactly like Qwen3, so ``fix_prefix`` is inherited.

    ``chat_template_kwargs`` is where the caller pins template variables that
    change the rendered prompt — for Qwen3.8 ``reasoning_effort``
    (``xhigh``/``medium``/``low``; the template default is ``xhigh``) and
    ``preserve_thinking``. They are part of the tokenizer identity the record
    carries via ``chat_template_sha256`` only indirectly, so a gateway serving
    one experiment must run with one fixed set.
    """

    reasoning_parser = "qwen3"
    tool_call_parser = "qwen3_coder"

    def _synthetic_base(self) -> list[dict[str, Any]]:
        return [_DUMMY_SYSTEM, _DUMMY_USER]

    def _tokenize_tool_segment(
        self, appended_messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> list[int]:
        return self._tokenize_rendered_suffix(
            [*self._synthetic_base(), _build_dummy_assistant(appended_messages)], appended_messages, tools=tools
        )

    def _tokenize_user_and_system_segment(
        self, appended_message: dict[str, Any], tools: list[dict[str, Any]] | None = None
    ) -> list[int]:
        if appended_message.get("role") == "system":
            raise ValueError(
                "the Qwen3.5/3.8 chat template only accepts a system message at position 0; "
                "appending one mid-conversation cannot be tokenized incrementally"
            )
        return self._tokenize_rendered_suffix(self._synthetic_base(), [appended_message], tools=tools)


_QWEN3_FIXED = "qwen3_fixed.jinja"
SUPPORTED_TOKENIZER_TYPES = ("default", "qwen3", "qwen3_5")


def get_tito_tokenizer(
    tokenizer: Any,
    tokenizer_type: str = "qwen3",
    *,
    allowed_append_roles: tuple[str, ...] = ("tool",),
    chat_template_kwargs: dict[str, Any] | None = None,
) -> TITOTokenizer:
    """Build a TITO tokenizer. `default` uses the tokenizer's own chat template
    (model-agnostic); `qwen3` loads the bundled fixed template (and disables thinking
    clearing when `user` appends are allowed, so earlier turns keep their reasoning);
    `qwen3_5` uses the tokenizer's own Qwen3.5/3.8 template with the synthetic-context
    fix that family needs. `chat_template_kwargs` are extra template variables pinned
    for every render (e.g. Qwen3.8 ``reasoning_effort``); for `qwen3` they merge on
    top of the fixed-template override."""
    if tokenizer is None:
        raise ValueError("tokenizer must not be None")
    roles = frozenset(allowed_append_roles)
    invalid = roles - _VALID_ROLES
    if invalid:
        raise ValueError(f"unknown roles in allowed_append_roles: {sorted(invalid)}; valid: {sorted(_VALID_ROLES)}")
    extra = dict(chat_template_kwargs or {})
    if "chat_template" in extra:
        raise ValueError("chat_template_kwargs must not carry 'chat_template'; use --chat-template-path")

    if tokenizer_type == "default":
        return TITOTokenizer(tokenizer, chat_template_kwargs=extra, allowed_append_roles=list(allowed_append_roles))
    if tokenizer_type == "qwen3":
        kw: dict[str, Any] = {"chat_template": (TEMPLATE_DIR / _QWEN3_FIXED).read_text()}
        if "user" in roles:
            kw["clear_thinking"] = False
        kw.update(extra)
        return Qwen3TITOTokenizer(tokenizer, chat_template_kwargs=kw, allowed_append_roles=list(allowed_append_roles))
    if tokenizer_type == "qwen3_5":
        return Qwen3_5TITOTokenizer(
            tokenizer, chat_template_kwargs=extra, allowed_append_roles=list(allowed_append_roles)
        )
    supported = ", ".join(repr(t) for t in SUPPORTED_TOKENIZER_TYPES)
    raise ValueError(f"unsupported tokenizer_type {tokenizer_type!r}; supported: {supported}")
