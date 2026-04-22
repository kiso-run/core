"""Tests for ``kiso.mcp.validate.validate_mcp_args``.

Business requirement: the worker pre-validates MCP-task arguments
against the server's cached ``input_schema`` before dispatching to
the MCP subprocess. Schema-bad args never reach the subprocess —
they fail fast with a human-readable error message that the
reviewer / replan path can turn into a concrete repair.

Contract:
- ``validate_mcp_args(schema, args) -> list[str]`` returns an empty
  list when args satisfy the schema.
- Each error string identifies the offending field in a way a
  downstream LLM can map back to the schema (``query`` is required,
  ``max_results`` must be integer, etc.).
- Missing / empty / non-object schema is treated as "no constraint"
  — validation passes (no errors). This keeps the gate permissive
  for servers that publish empty schemas.
- The function never raises; it returns errors as data.
"""

from __future__ import annotations

import pytest

from kiso.mcp.validate import validate_mcp_args


class TestValidArgs:
    def test_all_fields_present_and_typed(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        }
        assert validate_mcp_args(schema, {"query": "foo", "max_results": 5}) == []

    def test_optional_field_omitted(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        }
        assert validate_mcp_args(schema, {"query": "foo"}) == []

    def test_empty_schema_passes(self):
        assert validate_mcp_args({}, {"anything": "goes"}) == []

    def test_none_schema_passes(self):
        assert validate_mcp_args(None, {"anything": "goes"}) == []

    def test_schema_with_no_properties_passes(self):
        assert validate_mcp_args({"type": "object"}, {"x": 1}) == []


class TestMissingRequired:
    def test_single_missing_required_reports_field_name(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        }
        errors = validate_mcp_args(schema, {"max_results": 5})
        assert len(errors) == 1
        assert "query" in errors[0]
        assert "required" in errors[0].lower()

    def test_multiple_missing_required_all_reported(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
            },
            "required": ["a", "b"],
        }
        errors = validate_mcp_args(schema, {})
        assert len(errors) == 2
        joined = " ".join(errors)
        assert "a" in joined and "b" in joined


class TestWrongType:
    def test_integer_got_string(self):
        schema = {
            "type": "object",
            "properties": {"max_results": {"type": "integer"}},
        }
        errors = validate_mcp_args(schema, {"max_results": "five"})
        assert len(errors) == 1
        assert "max_results" in errors[0]
        assert "integer" in errors[0].lower()

    def test_string_got_number(self):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
        errors = validate_mcp_args(schema, {"query": 42})
        assert len(errors) == 1
        assert "query" in errors[0]
        assert "string" in errors[0].lower()

    def test_boolean_got_string(self):
        schema = {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
        }
        errors = validate_mcp_args(schema, {"enabled": "yes"})
        assert len(errors) == 1
        assert "enabled" in errors[0]


class TestCompositeErrors:
    def test_missing_required_and_wrong_type_both_reported(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        }
        errors = validate_mcp_args(schema, {"max_results": "five"})
        # One for missing 'query', one for wrong type 'max_results'.
        assert len(errors) >= 2
        joined = " ".join(errors)
        assert "query" in joined
        assert "max_results" in joined


class TestUnknownOrMalformedSchema:
    def test_malformed_schema_reports_error_not_crashes(self):
        schema = {"type": 42}  # type must be a string or list
        errors = validate_mcp_args(schema, {"x": 1})
        assert errors
        assert "input_schema" in errors[0]

    def test_non_dict_args_reports_error(self):
        schema = {"type": "object"}
        errors = validate_mcp_args(schema, "not a dict")  # type: ignore[arg-type]
        assert errors
        assert isinstance(errors, list)
