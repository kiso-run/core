"""Tests for the planner-side MCP integration.

Covers:
- TASK_TYPE_MCP constant exposed, "mcp" accepted by PLAN_SCHEMA type enum
- BRIEFER_SCHEMA includes the required mcp_methods field
- validate_plan semantic checks for mcp tasks: server/method required,
  server must be in the available pool, method must exist on server,
  args validated against inputSchema via jsonschema
- Cross-type hygiene: non-mcp tasks may not carry server/method
- briefer_mcp_method_filter_threshold setting default
- Planner role prompt contains the no-registry hard rule + secret
  refusal pattern (string assertions on kiso/roles/planner.md)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.brain.common import BRIEFER_SCHEMA, PLAN_SCHEMA, TASK_TYPE_MCP, TASK_TYPES
from kiso.brain.planner import validate_plan
from kiso.mcp.schemas import MCPMethod


def _method(server: str, name: str, input_schema: dict | None = None) -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description="",
        input_schema=input_schema or {"type": "object"},
        output_schema=None,
        annotations=None,
    )


_FINAL_MSG = {
    "type": "msg",
    "detail": "report the result",
    "wrapper": None,
    "args": None,
    "expect": None,
}


def _plan(tasks: list[dict]) -> dict:
    # Append a closing msg so validate_plan's ordering rule ("last task
    # must be msg or replan") does not interfere with the MCP-specific
    # tests below.
    if not tasks or tasks[-1].get("type") not in ("msg", "replan"):
        tasks = list(tasks) + [dict(_FINAL_MSG)]
    return {
        "goal": "test",
        "secrets": None,
        "tasks": tasks,
        "extend_replan": None,
        "needs_install": None,
        "knowledge": None,
        "kb_answer": None,
    }


def _mcp_task(server="github", method="create_issue", args=None, **overrides) -> dict:
    base = {
        "type": "mcp",
        "detail": "open an issue",
        "wrapper": None,
        "args": args or {"title": "bug", "body": "x"},
        "expect": "issue created",
        "server": server,
        "method": method,
    }
    base.update(overrides)
    return base


class TestConstants:
    def test_task_type_mcp_exported(self):
        assert TASK_TYPE_MCP == "mcp"
        assert TASK_TYPE_MCP in TASK_TYPES

    def test_plan_schema_accepts_mcp_type(self):
        enum = PLAN_SCHEMA["json_schema"]["schema"]["properties"]["tasks"]["items"][
            "properties"
        ]["type"]["enum"]
        assert "mcp" in enum

    def test_plan_schema_has_server_and_method_fields(self):
        props = PLAN_SCHEMA["json_schema"]["schema"]["properties"]["tasks"]["items"][
            "properties"
        ]
        assert "server" in props
        assert "method" in props

    def test_briefer_schema_has_mcp_methods_required(self):
        sch = BRIEFER_SCHEMA["json_schema"]["schema"]
        assert "mcp_methods" in sch["properties"]
        assert "mcp_methods" in sch["required"]


class TestValidateMCPTask:
    def test_happy_with_pool(self):
        pool = {
            "github": [
                _method(
                    "github",
                    "create_issue",
                    {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["title", "body"],
                    },
                )
            ],
        }
        errors = validate_plan(
            _plan([_mcp_task()]),
            mcp_methods_pool=pool,
        )
        assert errors == []

    def test_no_pool_still_passes_basic_shape(self):
        """Without a methods pool, the planner can't verify server/method
        existence but the basic shape (server+method present, wrapper null,
        expect present) is still validated."""
        errors = validate_plan(_plan([_mcp_task()]), mcp_methods_pool=None)
        assert errors == []

    def test_missing_server_rejected(self):
        errors = validate_plan(_plan([_mcp_task(server=None)]))
        assert any("server" in e for e in errors)

    def test_missing_method_rejected(self):
        errors = validate_plan(_plan([_mcp_task(method=None)]))
        assert any("method" in e for e in errors)

    def test_unknown_server_rejected(self):
        errors = validate_plan(
            _plan([_mcp_task(server="does-not-exist")]),
            mcp_methods_pool={"github": []},
        )
        assert any("does-not-exist" in e for e in errors)

    def test_unknown_method_rejected(self):
        errors = validate_plan(
            _plan([_mcp_task(method="does_not_exist")]),
            mcp_methods_pool={"github": [_method("github", "create_issue")]},
        )
        assert any("does_not_exist" in e for e in errors)

    def test_args_schema_violation_rejected(self):
        """jsonschema validates args against the method's inputSchema."""
        pool = {
            "github": [
                _method(
                    "github",
                    "create_issue",
                    {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                        },
                        "required": ["title", "body"],
                    },
                )
            ],
        }
        # Missing required 'body'
        errors = validate_plan(
            _plan([_mcp_task(args={"title": "bug"})]),
            mcp_methods_pool=pool,
        )
        assert any("body" in e for e in errors)

    def test_args_type_violation_rejected(self):
        pool = {
            "github": [
                _method(
                    "github",
                    "set_count",
                    {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                    },
                )
            ],
        }
        errors = validate_plan(
            _plan([_mcp_task(method="set_count", args={"n": "not an int"})]),
            mcp_methods_pool=pool,
        )
        assert any("integer" in e.lower() or "not" in e.lower() for e in errors)

    def test_resource_read_allowed_with_uri_arg(self):
        errors = validate_plan(
            _plan([_mcp_task(method="__resource_read", args={"uri": "kiso://x/1"})]),
        )
        assert errors == []

    def test_resource_read_without_uri_rejected(self):
        errors = validate_plan(
            _plan([_mcp_task(method="__resource_read", args={})]),
        )
        assert any("__resource_read" in e and "uri" in e for e in errors)

    def test_resource_read_with_non_string_uri_rejected(self):
        errors = validate_plan(
            _plan([_mcp_task(method="__resource_read", args={"uri": 42})]),
        )
        assert any("__resource_read" in e for e in errors)

    def test_resource_read_with_extra_args_rejected(self):
        errors = validate_plan(
            _plan([_mcp_task(
                method="__resource_read",
                args={"uri": "kiso://x/1", "extra": "bad"},
            )]),
        )
        assert any("__resource_read" in e and "extras" in e for e in errors)

    def test_resource_read_bypasses_methods_pool_check(self):
        """__resource_read is a synthetic method; it must not be
        rejected for "not existing" on the server's methods list."""
        errors = validate_plan(
            _plan([_mcp_task(method="__resource_read", args={"uri": "kiso://r/1"})]),
            mcp_methods_pool={"github": [_method("github", "create_issue")]},
        )
        assert errors == []

    def test_mcp_task_with_wrapper_field_rejected(self):
        task = _mcp_task()
        task["wrapper"] = "aider"
        errors = validate_plan(_plan([task]))
        assert any("wrapper=null" in e or "wrapper" in e for e in errors)

    def test_non_mcp_task_with_server_field_rejected(self):
        task = {
            "type": "exec",
            "detail": "echo hi",
            "wrapper": None,
            "args": None,
            "expect": "hi",
            "server": "github",  # stray
            "method": None,
        }
        errors = validate_plan(_plan([task]))
        assert any(
            "server=null" in e or "server/method" in e for e in errors
        )

    def test_mcp_task_missing_expect(self):
        task = _mcp_task()
        task["expect"] = None
        errors = validate_plan(_plan([task]))
        assert any("expect" in e for e in errors)


class TestPlannerRolePromptNoRegistryRule:
    PLANNER_MD = Path(__file__).resolve().parents[1] / "kiso" / "roles" / "planner.md"

    def test_planner_md_has_mcp_section(self):
        text = self.PLANNER_MD.read_text()
        assert "MCP" in text or "mcp" in text, (
            "planner.md must document the mcp task type"
        )

    def test_planner_md_has_no_registry_rule(self):
        """The planner prompt must forbid guessing at MCP server URLs."""
        text = self.PLANNER_MD.read_text()
        # Expect an explicit marker phrase
        markers = [
            "not maintain a registry",
            "NEVER guess",
            "concrete URL",
        ]
        matched = [m for m in markers if m.lower() in text.lower()]
        assert matched, (
            f"planner.md must contain the MCP no-registry rule; none of {markers} matched"
        )

    def test_planner_md_has_secret_refusal_rule(self):
        text = self.PLANNER_MD.read_text()
        markers = ["secret", "token"]
        assert any(m in text.lower() for m in markers), (
            "planner.md must mention the secret refusal pattern for MCP"
        )


class TestSettingDefault:
    def test_briefer_mcp_method_filter_threshold_default_10(self):
        from kiso.config import SETTINGS_DEFAULTS

        threshold = dict(SETTINGS_DEFAULTS)["briefer_mcp_method_filter_threshold"]
        assert threshold == 10
