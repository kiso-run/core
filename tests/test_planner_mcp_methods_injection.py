"""M1371: planner exposes MCP methods to the LLM when present.

M1370 wired `mcp_methods` into the briefer's context_pool so the
briefer can SELECT MCP methods. M1371 closes the second half:
the planner LLM must also SEE the MCP method catalog when making
routing decisions, otherwise the briefer's selections vanish into
the void and the planner falls back to wrappers/exec.

These tests assert that:

- Calling `build_planner_messages` with `mcp_catalog_text=...`
  results in planner messages whose user content contains a
  `## MCP Methods` (or equivalent) section listing the catalog.
- The section survives the briefer code path (briefing returned)
  AND the fallback path (no briefer / briefer disabled).
- An empty/None catalog text leaves no MCP Methods section.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiso.brain import build_planner_messages
from kiso.config import Config, Provider
from kiso.store import create_session, init_db
from tests.conftest import full_models, full_settings


def _config(briefer_enabled: bool = True) -> Config:
    return Config(
        tokens={"cli": "tok"},
        providers={
            "openrouter": Provider(base_url="https://api.example.com/v1")
        },
        users={},
        models=full_models(),
        settings=full_settings(
            context_messages=3, briefer_enabled=briefer_enabled
        ),
        raw={},
    )


def _briefing(**overrides) -> dict:
    base = {
        "modules": [],
        "skills": [],
        "mcp_methods": [],
        "context": "",
        "output_indices": [],
        "relevant_tags": [],
        "relevant_entities": [],
    }
    base.update(overrides)
    return base


@pytest.fixture()
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    await create_session(conn, "sess1")
    yield conn
    await conn.close()


_CATALOG_TEXT = (
    "- filesystem:read_file — Read a file from disk\n"
    "- filesystem:list_directory — List files in a directory\n"
    "- github:create_issue — Open a new issue in a repository"
)


class TestMcpMethodsInjectedIntoPlannerContext:
    """The planner LLM sees the MCP catalog when one is provided."""

    async def test_catalog_appears_in_planner_user_content_briefer_path(
        self, db
    ):
        briefing = _briefing(
            mcp_methods=["filesystem:read_file"],
            context="User wants to read a file via the filesystem MCP.",
        )

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(briefing)
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), patch(
            "kiso.brain.discover_wrappers", return_value=[]
        ):
            msgs, _, _ = await build_planner_messages(
                db,
                _config(briefer_enabled=True),
                "sess1",
                "user",
                "read /workspace/foo.txt",
                mcp_catalog_text=_CATALOG_TEXT,
            )

        user_content = msgs[1]["content"]
        assert "MCP Methods" in user_content, (
            "planner user content must contain a 'MCP Methods' section "
            "when mcp_catalog_text is provided. Otherwise the planner LLM "
            "cannot route a goal to an MCP method."
        )
        assert "filesystem:read_file" in user_content
        assert "filesystem:list_directory" in user_content
        assert "github:create_issue" in user_content

    async def test_catalog_appears_in_fallback_path(self, db):
        """Even with briefer disabled, the catalog reaches the planner."""

        with patch("kiso.brain.discover_wrappers", return_value=[]):
            msgs, _, _ = await build_planner_messages(
                db,
                _config(briefer_enabled=False),
                "sess1",
                "user",
                "read /workspace/foo.txt",
                mcp_catalog_text=_CATALOG_TEXT,
            )

        user_content = msgs[1]["content"]
        assert "MCP Methods" in user_content
        assert "filesystem:read_file" in user_content

    async def test_no_catalog_no_section(self, db):
        """When mcp_catalog_text is None, no MCP Methods section appears."""

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(_briefing())
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), patch(
            "kiso.brain.discover_wrappers", return_value=[]
        ):
            msgs, _, _ = await build_planner_messages(
                db,
                _config(),
                "sess1",
                "user",
                "what time is it",
                mcp_catalog_text=None,
            )

        user_content = msgs[1]["content"]
        assert "## MCP Methods" not in user_content

    async def test_empty_catalog_no_section(self, db):
        """An empty string catalog also yields no section."""

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps(_briefing())
            return "{}"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), patch(
            "kiso.brain.discover_wrappers", return_value=[]
        ):
            msgs, _, _ = await build_planner_messages(
                db,
                _config(),
                "sess1",
                "user",
                "what time is it",
                mcp_catalog_text="",
            )

        user_content = msgs[1]["content"]
        assert "## MCP Methods" not in user_content
