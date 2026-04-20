"""M1505: planner.md routing module rewritten as ``skills_and_mcp``.

After v0.10's collapse to ``exec | mcp | msg | replan``, the planner
prompt can no longer reason about wrappers, recipes, or the search
task type. These tests pin:

- The briefer's available-module set no longer advertises the retired
  wrapper/recipe modules and advertises ``skills_and_mcp`` instead.
- ``kiso/roles/planner.md`` declares the new module, drops the old
  ones, and no longer lists ``wrapper`` or ``search`` as task types.
"""
from __future__ import annotations

import re
from pathlib import Path

from kiso.brain.common import BRIEFER_MODULES, _BRIEFER_MODULE_DESCRIPTIONS

PLANNER_MD = Path(__file__).resolve().parents[1] / "kiso" / "roles" / "planner.md"


def _planner_text() -> str:
    return PLANNER_MD.read_text(encoding="utf-8")


def _module_body(text: str, name: str) -> str | None:
    """Return the body of a ``<!-- MODULE: name -->`` block, or None."""
    pattern = (
        rf"<!--\s*MODULE:\s*{re.escape(name)}\s*-->"
        r"(.*?)"
        r"(?=<!--\s*MODULE:|\Z)"
    )
    m = re.search(pattern, text, flags=re.DOTALL)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# BRIEFER module registry
# ---------------------------------------------------------------------------

class TestBrieferModuleSet:
    def test_retired_modules_removed(self):
        retired = {"kiso_native", "wrappers_rules", "wrapper_recovery"}
        assert retired.isdisjoint(BRIEFER_MODULES), (
            f"Retired modules still advertised: "
            f"{retired & BRIEFER_MODULES}"
        )

    def test_skills_and_mcp_module_advertised(self):
        assert "skills_and_mcp" in BRIEFER_MODULES

    def test_descriptions_kept_in_sync(self):
        for name in BRIEFER_MODULES:
            assert name in _BRIEFER_MODULE_DESCRIPTIONS, (
                f"Module {name!r} lacks a description"
            )
        for name in list(_BRIEFER_MODULE_DESCRIPTIONS):
            assert name in BRIEFER_MODULES, (
                f"Description for unknown module {name!r}"
            )

    def test_skills_and_mcp_description_mentions_both_primitives(self):
        desc = _BRIEFER_MODULE_DESCRIPTIONS.get("skills_and_mcp", "").lower()
        assert "skill" in desc
        assert "mcp" in desc


# ---------------------------------------------------------------------------
# planner.md: new module exists, old ones removed
# ---------------------------------------------------------------------------

class TestPlannerMdModules:
    def test_new_skills_and_mcp_module_present(self):
        body = _module_body(_planner_text(), "skills_and_mcp")
        assert body is not None, "Missing MODULE: skills_and_mcp block"
        assert body.strip(), "skills_and_mcp block is empty"

    def test_retired_module_headers_absent(self):
        text = _planner_text()
        for retired in ("kiso_native", "wrappers_rules", "wrapper_recovery"):
            assert (
                f"MODULE: {retired}" not in text
            ), f"Retired module header still in planner.md: {retired}"

    def test_no_inline_mcp_module_duplicate(self):
        """The old ``<!-- MODULE: mcp -->`` block is absorbed into
        ``skills_and_mcp``; there must not be both."""
        body = _module_body(_planner_text(), "mcp")
        assert body is None, "Legacy MODULE: mcp block should be folded into skills_and_mcp"


# ---------------------------------------------------------------------------
# planner.md core: task-type list no longer lists wrapper/search
# ---------------------------------------------------------------------------

class TestPlannerMdCoreTaskTypes:
    def test_core_task_type_list_has_no_wrapper_entry(self):
        core = _module_body(_planner_text(), "core")
        assert core is not None
        # Look for a task-type bullet specifically — generic references
        # to "wrapper args" or "browser wrapper" elsewhere are out of
        # core's scope. The core module enumerates task types as
        # ``- <type>: ...`` bullets.
        for line in core.splitlines():
            stripped = line.lstrip("- ").strip()
            if stripped.lower().startswith("wrapper:"):
                raise AssertionError(
                    "Core module still lists wrapper as a task type: " + line
                )
            if stripped.lower().startswith("search:"):
                raise AssertionError(
                    "Core module still lists search as a task type: " + line
                )

    def test_core_task_type_list_mentions_mcp(self):
        core = _module_body(_planner_text(), "core") or ""
        assert "mcp:" in core.lower(), (
            "Core module must describe the mcp task type"
        )


# ---------------------------------------------------------------------------
# skills_and_mcp content: routing + install-from-URL
# ---------------------------------------------------------------------------

class TestSkillsAndMcpContent:
    def test_covers_both_install_from_url_commands(self):
        body = (_module_body(_planner_text(), "skills_and_mcp") or "").lower()
        assert "kiso mcp install --from-url" in body
        assert "kiso skill install --from-url" in body

    def test_declares_no_registry_rule(self):
        body = (_module_body(_planner_text(), "skills_and_mcp") or "").lower()
        assert "does not maintain a registry" in body or "no registry" in body, (
            "skills_and_mcp must state Kiso maintains no registry of "
            "MCP servers or skills"
        )

    def test_mentions_needs_install_lifecycle(self):
        body = (_module_body(_planner_text(), "skills_and_mcp") or "")
        assert "needs_install" in body
