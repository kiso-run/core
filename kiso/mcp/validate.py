"""Pre-flight validation of MCP method arguments against ``input_schema``.

Returns errors as strings so the caller can build a replan reason
without depending on exception types.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema import Draft202012Validator


def validate_mcp_args(schema: Any, args: Any) -> list[str]:
    """Validate *args* against the MCP method's JSON Schema *schema*.

    Missing, empty, or non-dict schema is "no constraint" (permissive).
    Never raises — schema errors are returned as strings.
    """
    if not isinstance(schema, dict) or not schema:
        return []
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]

    try:
        validator = Draft202012Validator(schema)
    except jsonschema.SchemaError as exc:
        return [f"input_schema is invalid: {exc.message}"]

    errors: list[str] = []
    try:
        for err in validator.iter_errors(args):
            errors.append(_format_error(err))
    except (jsonschema.SchemaError, TypeError) as exc:
        # TypeError is raised by malformed schema fields
        # (e.g. ``{"type": 42}``) that jsonschema doesn't reject at
        # validator construction time.
        return [f"input_schema could not be evaluated: {exc}"]
    return errors


def _format_error(err: jsonschema.ValidationError) -> str:
    """Turn a ``jsonschema.ValidationError`` into a planner-friendly line."""
    field = ".".join(str(p) for p in err.absolute_path)
    if err.validator == "required":
        missing = err.message.split("'")[1] if "'" in err.message else err.message
        return f"{missing!r} is required"
    if err.validator == "type":
        expected = err.validator_value
        if isinstance(expected, list):
            expected_str = " or ".join(expected)
        else:
            expected_str = str(expected)
        got = type(err.instance).__name__
        return f"{field or '<root>'} must be {expected_str} (got {got})"
    if field:
        return f"{field}: {err.message}"
    return err.message
