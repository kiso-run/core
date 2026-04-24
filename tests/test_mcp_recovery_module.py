"""M1543 — the `mcp_recovery` planner-prompt module.

The module is selectable by the briefer when it sees MCP servers
flagged unhealthy, and its body is injected into the planner prompt
only when selected. Validates:

- registry entries (``BRIEFER_MODULES`` + description) are present,
- the marker exists in ``kiso/roles/planner.md``,
- the module body is included in the rendered prompt when selected,
- the module body is absent when not selected.
"""

from __future__ import annotations

from pathlib import Path


PLANNER_MD = (
    Path(__file__).resolve().parent.parent
    / "kiso" / "roles" / "planner.md"
)


class TestModuleRegistered:
    def test_in_briefer_modules(self) -> None:
        from kiso.brain.common import BRIEFER_MODULES
        assert "mcp_recovery" in BRIEFER_MODULES

    def test_has_description(self) -> None:
        from kiso.brain.common import _BRIEFER_MODULE_DESCRIPTIONS
        desc = _BRIEFER_MODULE_DESCRIPTIONS["mcp_recovery"]
        assert desc
        # One-line cap for the briefer prompt.
        assert "\n" not in desc
        assert len(desc) < 80


class TestPlannerMarker:
    def test_marker_present(self) -> None:
        text = PLANNER_MD.read_text(encoding="utf-8")
        assert "<!-- MODULE: mcp_recovery -->" in text

    def test_body_mentions_unhealthy_and_alternatives(self) -> None:
        text = PLANNER_MD.read_text(encoding="utf-8")
        marker = "<!-- MODULE: mcp_recovery -->"
        idx = text.index(marker)
        next_marker = text.index("<!-- MODULE:", idx + 1)
        body = text[idx + len(marker):next_marker].lower()
        assert "unhealthy" in body
        assert "alternative" in body or "alternatives" in body
        assert "kiso mcp test" in body


class TestSelectionRendersModule:
    def test_body_included_when_selected(self) -> None:
        from kiso.brain.common import _load_modular_prompt

        rendered = _load_modular_prompt("planner", ["mcp_recovery"])
        # The canonical sentence from the prompt body
        assert "route around" in rendered.lower() or (
            "unhealthy" in rendered.lower()
        )

    def test_body_absent_when_not_selected(self) -> None:
        from kiso.brain.common import _load_modular_prompt

        rendered = _load_modular_prompt("planner", [])
        # "unhealthy" appears only inside the mcp_recovery body.
        assert "mcp_recovery" not in rendered.lower()
        # "route around" appears only in the mcp_recovery body.
        assert "route around" not in rendered.lower()
