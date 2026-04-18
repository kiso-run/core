"""Tests for kiso/skill_runtime.py — role-scoped skill projection.

Business requirement: project a :class:`Skill` into the slice each
runtime role needs.

- Briefer sees metadata only (name, description, when_to_use,
  audiences). Never the body.
- Planner / Worker / Reviewer / Messenger see their corresponding
  ``## Role`` section, OR the full body as planner-only fallback when
  role sections are absent.
- The ``audiences`` field acts as a gate: when present and a role
  isn't listed, that role's projection is empty.

Plus ``filter_by_activation_hints(skills, message)`` — deterministic
pre-filter applied before the briefer runs. Mirrors recipe
``applies_to``/``excludes`` semantics: single-token selectors match
word-bounded; multi-word selectors match substring (lowercased, space-
normalised).
"""

from __future__ import annotations

from kiso.skill_loader import Skill
from kiso.skill_runtime import (
    filter_by_activation_hints,
    instructions_for_messenger,
    instructions_for_planner,
    instructions_for_reviewer,
    instructions_for_worker,
    metadata_for_briefer,
)


def _make_skill(
    name: str = "x",
    description: str = "desc",
    body: str = "",
    role_sections: dict | None = None,
    audiences: list[str] | None = None,
    when_to_use: str | None = None,
    activation_hints: dict | None = None,
) -> Skill:
    return Skill(
        name=name,
        description=description,
        body=body,
        role_sections=role_sections or {},
        audiences=audiences,
        when_to_use=when_to_use,
        activation_hints=activation_hints,
    )


# ---------------------------------------------------------------------------
# Briefer metadata
# ---------------------------------------------------------------------------


class TestBrieferMetadata:
    def test_basic_fields(self):
        s = _make_skill(
            name="foo",
            description="d",
            when_to_use="use when X",
            audiences=["planner"],
        )
        meta = metadata_for_briefer(s)
        assert meta["name"] == "foo"
        assert meta["description"] == "d"
        assert meta["when_to_use"] == "use when X"
        assert meta["audiences"] == ["planner"]

    def test_body_never_exposed(self):
        s = _make_skill(body="SECRET body content")
        meta = metadata_for_briefer(s)
        assert "body" not in meta
        assert "SECRET" not in str(meta)

    def test_optional_fields_absent(self):
        s = _make_skill()
        meta = metadata_for_briefer(s)
        assert meta == {"name": "x", "description": "desc"}


# ---------------------------------------------------------------------------
# Role projections
# ---------------------------------------------------------------------------


class TestRoleProjections:
    def test_planner_uses_role_section_when_present(self):
        s = _make_skill(
            body="ignored fallback",
            role_sections={"planner": "planner block"},
        )
        assert instructions_for_planner(s) == "planner block"

    def test_planner_falls_back_to_body_when_no_role_sections(self):
        s = _make_skill(body="whole body here")
        assert instructions_for_planner(s) == "whole body here"

    def test_worker_section(self):
        s = _make_skill(role_sections={"worker": "run tests"})
        assert instructions_for_worker(s) == "run tests"

    def test_worker_has_no_fallback(self):
        # Worker/reviewer/messenger do NOT fall back to full body —
        # only planner does. Bodies without role sections are
        # planner-only by convention.
        s = _make_skill(body="body only")
        assert instructions_for_worker(s) == ""
        assert instructions_for_reviewer(s) == ""
        assert instructions_for_messenger(s) == ""

    def test_reviewer_section(self):
        s = _make_skill(role_sections={"reviewer": "check output"})
        assert instructions_for_reviewer(s) == "check output"

    def test_messenger_section(self):
        s = _make_skill(role_sections={"messenger": "be terse"})
        assert instructions_for_messenger(s) == "be terse"


# ---------------------------------------------------------------------------
# Audiences gating
# ---------------------------------------------------------------------------


class TestAudiencesGating:
    def test_audiences_absent_means_all_roles(self):
        s = _make_skill(
            role_sections={
                "planner": "P",
                "worker": "W",
                "reviewer": "R",
                "messenger": "M",
            }
        )
        assert instructions_for_planner(s) == "P"
        assert instructions_for_worker(s) == "W"
        assert instructions_for_reviewer(s) == "R"
        assert instructions_for_messenger(s) == "M"

    def test_audiences_restrict_projection(self):
        s = _make_skill(
            role_sections={"planner": "P", "worker": "W"},
            audiences=["planner"],
        )
        assert instructions_for_planner(s) == "P"
        assert instructions_for_worker(s) == ""  # excluded

    def test_audiences_applies_to_body_fallback_too(self):
        # Planner fallback from body also gated by audiences.
        s = _make_skill(body="body", audiences=["worker"])
        assert instructions_for_planner(s) == ""


# ---------------------------------------------------------------------------
# Activation hints pre-filter
# ---------------------------------------------------------------------------


class TestActivationHints:
    def test_no_hints_passes_through(self):
        s = _make_skill(name="a")
        assert filter_by_activation_hints([s], "any message") == [s]

    def test_applies_to_single_token_word_bounded(self):
        s = _make_skill(
            name="py",
            activation_hints={"applies_to": ["python"], "excludes": []},
        )
        assert filter_by_activation_hints([s], "python error") == [s]
        assert filter_by_activation_hints([s], "help with pythonista") == []
        assert filter_by_activation_hints([s], "js only") == []

    def test_applies_to_multi_word_substring(self):
        s = _make_skill(
            name="pd",
            activation_hints={"applies_to": ["data analysis"], "excludes": []},
        )
        assert (
            filter_by_activation_hints([s], "do some data analysis") == [s]
        )
        assert filter_by_activation_hints([s], "data alone") == []

    def test_excludes_filter(self):
        s = _make_skill(
            name="s",
            activation_hints={"applies_to": [], "excludes": ["javascript"]},
        )
        assert filter_by_activation_hints([s], "pure python") == [s]
        assert filter_by_activation_hints([s], "javascript issue") == []

    def test_excludes_beats_applies_to(self):
        s = _make_skill(
            name="s",
            activation_hints={
                "applies_to": ["python"],
                "excludes": ["deprecated"],
            },
        )
        assert (
            filter_by_activation_hints([s], "python deprecated api") == []
        )

    def test_empty_applies_to_is_open(self):
        s = _make_skill(
            name="s",
            activation_hints={"applies_to": [], "excludes": []},
        )
        assert filter_by_activation_hints([s], "anything") == [s]

    def test_case_insensitive(self):
        s = _make_skill(
            name="s",
            activation_hints={"applies_to": ["Python"], "excludes": []},
        )
        assert filter_by_activation_hints([s], "PYTHON issue") == [s]

    def test_mix_of_filtered_and_kept(self):
        keep = _make_skill(name="k", activation_hints={"applies_to": ["go"], "excludes": []})
        drop = _make_skill(name="d", activation_hints={"applies_to": ["rust"], "excludes": []})
        always = _make_skill(name="a")
        out = filter_by_activation_hints([keep, drop, always], "go generics")
        names = [s.name for s in out]
        assert names == ["k", "a"]

    def test_empty_message_keeps_all(self):
        s = _make_skill(
            name="s",
            activation_hints={"applies_to": ["python"], "excludes": []},
        )
        # No message to match against → permissive.
        assert filter_by_activation_hints([s], "") == [s]


class TestActivationHintsReplanBypass:
    """M1538: ``is_replan=True`` disables the filter."""

    def test_replan_keeps_non_matching_skill(self):
        s = _make_skill(
            name="s",
            activation_hints={"applies_to": ["python"], "excludes": []},
        )
        # Message has no "python" — normally filtered out.
        assert filter_by_activation_hints([s], "fix rust bug") == []
        # But replan bypasses the filter entirely.
        assert (
            filter_by_activation_hints([s], "fix rust bug", is_replan=True) == [s]
        )

    def test_replan_keeps_excluded_skill(self):
        s = _make_skill(
            name="s",
            activation_hints={"applies_to": [], "excludes": ["secret"]},
        )
        assert filter_by_activation_hints([s], "secret") == []
        assert (
            filter_by_activation_hints([s], "secret", is_replan=True) == [s]
        )
