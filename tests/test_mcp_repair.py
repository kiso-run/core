"""M1547 — one-shot LLM-mediated repair for invalid MCP args.

When the planner emits an MCP task whose args fail the method's
input-schema pre-flight validation, kiso calls a tiny LLM role
(``mcp_repair``) with the task detail, the schema, and the failing
args; the role returns a revised args object. If the revision
still fails validation, we give up and fall through to the
existing replan path.

The role prompt lives at ``kiso/roles/mcp_repair.md``. The
public entry point is
``kiso.brain.mcp_repair.repair_mcp_args(config, detail, schema,
failing_args)``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


class TestRoleFileShipped:
    def test_mcp_repair_md_exists(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "kiso" / "roles" / "mcp_repair.md"
        )
        assert path.is_file()

    def test_role_mentions_json_object_output(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "kiso" / "roles" / "mcp_repair.md"
        )
        text = path.read_text(encoding="utf-8").lower()
        assert "json object" in text or "json_object" in text
        # Must flag that it's a repair — not an arbitrary shell-out.
        assert "args" in text
        assert "schema" in text


class TestRepairMcpArgs:
    async def test_successful_repair_returns_valid_args(self) -> None:
        from kiso.brain.mcp_repair import repair_mcp_args
        from tests.conftest import make_config

        schema = {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }
        failing = {}  # missing required field

        async def _fake_call(config, role, messages, **kwargs):
            assert role == "mcp_repair" or role == "worker"
            return json.dumps({"message": "hello"})

        with patch("kiso.brain.mcp_repair.call_llm", new=AsyncMock(side_effect=_fake_call)):
            result = await repair_mcp_args(
                config=make_config(),
                detail="Send a friendly hello",
                schema=schema,
                failing_args=failing,
            )
        assert result == {"message": "hello"}

    async def test_non_object_response_rejected(self) -> None:
        from kiso.brain.mcp_repair import repair_mcp_args
        from tests.conftest import make_config

        schema = {"type": "object"}
        async def _fake_call(*args, **kwargs):
            return '"not an object"'

        with patch("kiso.brain.mcp_repair.call_llm", new=AsyncMock(side_effect=_fake_call)):
            result = await repair_mcp_args(
                config=make_config(),
                detail="x",
                schema=schema,
                failing_args={},
            )
        assert result is None

    async def test_malformed_json_response_rejected(self) -> None:
        from kiso.brain.mcp_repair import repair_mcp_args
        from tests.conftest import make_config

        async def _fake_call(*args, **kwargs):
            return "not json at all"

        with patch("kiso.brain.mcp_repair.call_llm", new=AsyncMock(side_effect=_fake_call)):
            result = await repair_mcp_args(
                config=make_config(),
                detail="x",
                schema={"type": "object"},
                failing_args={},
            )
        assert result is None


class TestRolesRegistryIncludesMcpRepair:
    def test_mcp_repair_in_registry(self) -> None:
        from kiso.brain.roles_registry import ROLES

        assert "mcp_repair" in ROLES
        assert ROLES["mcp_repair"].prompt_filename == "mcp_repair.md"
