"""Discovery + content invariants for the bundled
``message-attachment-receiver`` skill at
``plugins/message-attachment-receiver-skill/``.

The skill is shipped in-tree as a plugin and as the GitHub repo
``kiso-run/message-attachment-receiver-skill``. Its job is to
read user-uploaded attachments (audio / image / document) via
the right MCP and re-plan with the extracted content as if the
user had typed it inline.

These tests pin its surface so an accidental edit can't silently
break the routing rules or drop a category.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kiso.skill_loader import discover_skills, parse_skill_file


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILL_PATH = _REPO_ROOT / "plugins" / "message-attachment-receiver-skill"


@pytest.fixture(scope="module")
def skill():
    """Parse the bundled SKILL.md once."""
    skill_md = _SKILL_PATH / "SKILL.md"
    assert skill_md.is_file(), (
        f"message-attachment-receiver SKILL.md missing at {skill_md}"
    )
    return parse_skill_file(skill_md, bundled_root=_SKILL_PATH)


class TestFrontmatter:
    def test_name_is_canonical(self, skill):
        assert skill.name == "message-attachment-receiver"

    def test_description_present(self, skill):
        assert skill.description
        d = skill.description.lower()
        assert "attachment" in d or "uploaded" in d

    def test_when_to_use_mentions_uploads(self, skill):
        wt = (skill.when_to_use or "").lower()
        assert "upload" in wt or "attach" in wt

    def test_activation_hints_cover_all_categories(self, skill):
        hints = skill.activation_hints or {}
        applies = set(hints.get("applies_to") or [])
        for tag in ("audio", "image", "document"):
            assert tag in applies or "attachment" in applies, (
                f"activation_hints.applies_to should cover {tag} or "
                f"the umbrella `attachment`; got {applies}"
            )

    def test_version_advances_to_v0_2(self, skill):
        """Generalisation from voice-only → all attachments is the
        v0.1.0 → v0.2.0 bump."""
        assert skill.version == "0.2.0"


class TestPlannerGuidance:
    def test_planner_section_present(self, skill):
        assert "planner" in skill.role_sections

    def test_planner_routes_audio_to_transcriber(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "kiso-transcriber" in body
        assert "transcribe_audio" in body

    def test_planner_routes_image_to_ocr(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "kiso-ocr" in body
        assert "ocr_image" in body
        assert "describe_image" in body

    def test_planner_routes_document_to_docreader(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "kiso-docreader" in body
        assert "read_document" in body

    def test_planner_lists_extensions_for_each_category(self, skill):
        body = skill.role_sections["planner"].lower()
        for ext in (".mp3", ".m4a", ".wav", ".ogg"):
            assert ext in body, f"audio ext {ext} missing from planner"
        for ext in (".png", ".jpg", ".webp"):
            assert ext in body, f"image ext {ext} missing from planner"
        for ext in (".pdf", ".docx", ".csv"):
            assert ext in body, f"document ext {ext} missing from planner"

    def test_planner_uses_replan_loop(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "replan" in body

    def test_planner_forbids_one_shot_plan(self, skill):
        body = skill.role_sections["planner"].lower()
        assert (
            "two stages" in body or "two stage" in body
            or "never put the read tasks" in body
            or "never combine" in body
        )

    def test_planner_uses_parallel_group_for_multifile(self, skill):
        body = skill.role_sections["planner"].lower()
        assert "group" in body
        assert "parallel" in body or "concurrent" in body

    def test_planner_forbids_cross_routing(self, skill):
        body = skill.role_sections["planner"].lower()
        assert (
            "never to ocr" in body or "no cross-routing" in body
            or "goes only to" in body
        )

    def test_planner_handles_missing_mcp_with_msg_not_exec(self, skill):
        """When the briefer's catalog doesn't list the target server
        for an attachment category (e.g. user uploads audio but
        kiso-transcriber isn't installed), the planner must:
          1. emit a msg telling the user how to install,
          2. NOT fall back to exec / wrong-tool routing.

        This pins the rule in SKILL.md so a regression that drops
        either half is caught at test time, before the planner sees
        a misleading prompt."""
        import re
        # Collapse whitespace so a line break inside the rule sentence
        # doesn't hide the keyword pair.
        body = re.sub(r"\s+", " ", skill.role_sections["planner"].lower())
        names_failure = (
            "does not list" in body or "not available" in body
            or "missing" in body or "mismatched" in body
        )
        assert names_failure, (
            "planner section must name the missing-MCP failure mode"
        )
        emits_msg = (
            "emit a `msg`" in body or "emit a msg" in body
            or "msg task" in body
        )
        assert emits_msg, (
            "planner section must instruct emitting a msg task when "
            "the required MCP isn't available"
        )
        suggests_install = "kiso mcp install" in body or "install" in body
        assert suggests_install, (
            "planner section must point the user at `kiso mcp install`"
        )
        forbids_exec_fallback = (
            "do not fall back to exec" in body
            or "no exec fallback" in body
            or "never fall back" in body
        )
        assert forbids_exec_fallback, (
            "planner section must forbid falling back to exec when an "
            "MCP is missing — the wrong tool would silently produce "
            "wrong content"
        )


class TestReviewerGuidance:
    def test_reviewer_section_present(self, skill):
        assert "reviewer" in skill.role_sections

    def test_reviewer_replans_on_empty_output(self, skill):
        body = skill.role_sections["reviewer"].lower()
        assert "empty" in body
        assert "replan" in body


class TestSkillIsDiscoverable:
    def test_discover_finds_skill(self, tmp_path):
        import shutil
        target = tmp_path / "message-attachment-receiver"
        shutil.copytree(_SKILL_PATH, target)
        skills = discover_skills(skills_dir=tmp_path)
        names = [s.name for s in skills]
        assert "message-attachment-receiver" in names
