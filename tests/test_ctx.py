"""Tests for agentix.ctx — schema validation, defaults, provides checking."""

from __future__ import annotations

import logging
import types

import pytest

from agentix.ctx import apply_defaults, extract_schema, validate_provides, validate_requires


SAMPLE_SCHEMA = {
    "requires": {
        "prompt": {"type": "str", "description": "The user prompt"},
        "count": {"type": "int", "description": "Number of items"},
    },
    "optional": {
        "temperature": {"type": "float", "default": 0.7},
        "verbose": {"type": "bool", "default": False},
    },
    "provides": {
        "answer": {"type": "str"},
        "tokens": {"type": "int"},
    },
}


def test_validate_requires_ok():
    """All required keys present with correct types."""
    ctx = {"prompt": "hello", "count": 5}
    validate_requires(ctx, SAMPLE_SCHEMA, "test")  # should not raise


def test_validate_requires_missing():
    """Missing required key raises ValueError."""
    ctx = {"prompt": "hello"}  # missing 'count'
    with pytest.raises(ValueError, match="count"):
        validate_requires(ctx, SAMPLE_SCHEMA, "test")


def test_validate_requires_wrong_type():
    """Wrong type raises TypeError."""
    ctx = {"prompt": "hello", "count": "not_an_int"}
    with pytest.raises(TypeError, match="int"):
        validate_requires(ctx, SAMPLE_SCHEMA, "test")


def test_apply_defaults():
    """Fills defaults for optional keys not present."""
    ctx = {"prompt": "hello"}
    apply_defaults(ctx, SAMPLE_SCHEMA)
    assert ctx["temperature"] == 0.7
    assert ctx["verbose"] is False


def test_apply_defaults_no_overwrite():
    """Does not overwrite existing values."""
    ctx = {"prompt": "hello", "temperature": 1.0}
    apply_defaults(ctx, SAMPLE_SCHEMA)
    assert ctx["temperature"] == 1.0


def test_validate_provides_warning(caplog):
    """Logs warning for missing output keys."""
    result = {"answer": "yes"}  # missing 'tokens'
    with caplog.at_level(logging.WARNING, logger="agentix.ctx"):
        validate_provides(result, SAMPLE_SCHEMA, "test")
    assert any("tokens" in r.message for r in caplog.records)


def test_extract_schema_none():
    """Module without CTX_SCHEMA returns None."""
    module = types.ModuleType("empty")
    assert extract_schema(module) is None


def test_extract_schema_present():
    """Module with CTX_SCHEMA returns the schema dict."""
    module = types.ModuleType("with_schema")
    module.CTX_SCHEMA = {"requires": {"x": {"type": "str"}}}
    assert extract_schema(module) == {"requires": {"x": {"type": "str"}}}
