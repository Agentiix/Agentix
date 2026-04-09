"""ctx schema contracts — validates plugin CTX_SCHEMA at the eval boundary.

Plugins declare schemas as plain dicts (no imports). The framework validates
inputs before run() and warns about missing outputs after run().
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agentix.ctx")

TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "dict": dict,
    "list": list,
}


def extract_schema(module) -> dict | None:
    """Read CTX_SCHEMA from plugin module. Returns None if not defined."""
    return getattr(module, "CTX_SCHEMA", None)


def validate_requires(ctx: dict, schema: dict, plugin_name: str) -> None:
    """Check all required keys are present and correctly typed."""
    for key, spec in schema.get("requires", {}).items():
        if key not in ctx:
            desc = spec.get("description", "N/A")
            raise ValueError(
                f"Plugin '{plugin_name}' requires ctx['{key}'] but it's missing. "
                f"Description: {desc}"
            )
        expected = TYPE_MAP.get(spec.get("type", ""))
        if expected and not isinstance(ctx[key], expected):
            raise TypeError(
                f"ctx['{key}'] must be {spec['type']}, "
                f"got {type(ctx[key]).__name__}"
            )


def apply_defaults(ctx: dict, schema: dict) -> dict:
    """Fill optional keys with defaults if not present."""
    for key, spec in schema.get("optional", {}).items():
        if key not in ctx and "default" in spec:
            ctx[key] = spec["default"]
    return ctx


def validate_provides(result: dict, schema: dict, plugin_name: str) -> None:
    """Warn if plugin didn't provide declared output keys."""
    for key in schema.get("provides", {}):
        if key not in result:
            logger.warning(
                "Plugin '%s' declared provides['%s'] but didn't return it",
                plugin_name,
                key,
            )
