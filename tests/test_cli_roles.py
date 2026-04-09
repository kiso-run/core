"""Tests for the M1292 `kiso roles` CLI command group and roles registry."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


class TestRolesRegistry:
    """The registry is the single source of truth for role metadata."""

    def test_every_model_metadata_entry_has_a_registry_entry(self):
        """Every model in _MODEL_METADATA must be referenced by ≥1 role."""
        from kiso.brain.roles_registry import list_roles
        from kiso.config import _MODEL_METADATA

        model_keys = {role for role, _model, _desc in _MODEL_METADATA}
        registry_model_keys = {r.model_key for r in list_roles()}
        # Every model is wired to at least one role
        missing = model_keys - registry_model_keys
        assert not missing, f"Model keys with no role: {missing}"

    def test_every_registry_entry_has_non_empty_description(self):
        from kiso.brain.roles_registry import list_roles
        for r in list_roles():
            assert r.description.strip(), f"Role {r.name} has empty description"

    def test_every_registry_entry_prompt_filename_resolves_to_a_bundled_file(self):
        from pathlib import Path

        from kiso.brain.roles_registry import list_roles

        bundled_dir = Path(__file__).resolve().parent.parent / "kiso" / "roles"
        for r in list_roles():
            assert (bundled_dir / r.prompt_filename).is_file(), (
                f"Role {r.name} -> bundled file {r.prompt_filename} missing"
            )

    def test_default_model_is_derived_from_model_metadata(self):
        """The registry must NEVER hard-code a model string — only derive it."""
        from kiso.brain.roles_registry import list_roles
        from kiso.config import MODEL_DEFAULTS

        for r in list_roles():
            assert r.default_model == MODEL_DEFAULTS[r.model_key], (
                f"{r.name}: default_model drift "
                f"({r.default_model} vs {MODEL_DEFAULTS[r.model_key]})"
            )

    def test_get_role_returns_none_for_unknown(self):
        from kiso.brain.roles_registry import get_role
        assert get_role("not_a_real_role") is None

    def test_inflight_classifier_present_and_shares_classifier_model(self):
        """inflight-classifier has its own .md but shares the classifier model."""
        from kiso.brain.roles_registry import get_role
        r = get_role("inflight-classifier")
        assert r is not None
        assert r.model_key == "classifier"


# ---------------------------------------------------------------------------
# CLI: kiso roles list / show / diff / reset
# ---------------------------------------------------------------------------


@pytest.fixture()
def role_dir(tmp_path):
    """Per-test KISO_DIR with a populated roles/ subdir from the bundle."""
    from cli.role import _package_roles
    rd = tmp_path / "roles"
    rd.mkdir()
    for name, content in _package_roles().items():
        (rd / f"{name}.md").write_text(content, encoding="utf-8")
    return tmp_path


class TestRolesList:
    def test_lists_every_registry_role_with_model(self, role_dir, capsys):
        from cli.roles import roles_list
        from kiso.brain.roles_registry import list_roles

        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_list(argparse.Namespace())
        out = capsys.readouterr().out
        for r in list_roles():
            assert r.name in out, f"role {r.name} missing from list output"
            assert r.default_model in out, f"model {r.default_model} missing"

    def test_marks_user_override_when_file_differs(self, role_dir, capsys):
        from cli.roles import roles_list

        target = role_dir / "roles" / "planner.md"
        target.write_text("CUSTOMIZED PLANNER PROMPT", encoding="utf-8")
        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_list(argparse.Namespace())
        out = capsys.readouterr().out
        # The line containing "planner" should mention "override"
        line = next(l for l in out.splitlines() if " planner " in f" {l} ")
        assert "override" in line.lower()

    def test_no_override_marker_when_file_matches_bundle(self, role_dir, capsys):
        from cli.roles import roles_list

        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_list(argparse.Namespace())
        out = capsys.readouterr().out
        line = next(l for l in out.splitlines() if " planner " in f" {l} ")
        assert "override" not in line.lower()


class TestRolesShow:
    def test_show_prints_user_override_when_present(self, role_dir, capsys):
        from cli.roles import roles_show

        target = role_dir / "roles" / "planner.md"
        target.write_text("MY OVERRIDE", encoding="utf-8")
        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_show(argparse.Namespace(name="planner"))
        out = capsys.readouterr().out
        assert "MY OVERRIDE" in out

    def test_show_prints_bundled_when_no_override(self, role_dir, capsys):
        from cli.role import _package_roles
        from cli.roles import roles_show

        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_show(argparse.Namespace(name="planner"))
        out = capsys.readouterr().out
        bundled = _package_roles()["planner"]
        # The first 60 chars of the bundled prompt should appear in output
        snippet = bundled.strip().splitlines()[0][:60]
        assert snippet in out

    def test_show_includes_model_in_header(self, role_dir, capsys):
        from cli.roles import roles_show
        from kiso.brain.roles_registry import get_role

        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_show(argparse.Namespace(name="planner"))
        out = capsys.readouterr().out
        assert get_role("planner").default_model in out

    def test_show_unknown_role_exits_nonzero(self, role_dir, capsys):
        from cli.roles import roles_show

        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            with pytest.raises(SystemExit):
                roles_show(argparse.Namespace(name="nonexistent"))
        err = capsys.readouterr().err
        assert "nonexistent" in err


class TestRolesDiff:
    def test_diff_no_override_message(self, role_dir, capsys):
        from cli.roles import roles_diff

        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_diff(argparse.Namespace(name="planner"))
        out = capsys.readouterr().out
        assert "no user override" in out.lower() or "matches bundled" in out.lower()

    def test_diff_returns_unified_diff_when_override_differs(self, role_dir, capsys):
        from cli.roles import roles_diff

        target = role_dir / "roles" / "planner.md"
        target.write_text("AAAAA-OVERRIDE-LINE\n", encoding="utf-8")
        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_diff(argparse.Namespace(name="planner"))
        out = capsys.readouterr().out
        assert "AAAAA-OVERRIDE-LINE" in out
        # Standard unified-diff markers
        assert "---" in out and "+++" in out


class TestRolesReset:
    def test_reset_overwrites_user_override_with_bundle(self, role_dir, capsys):
        from cli.role import _package_roles
        from cli.roles import roles_reset

        target = role_dir / "roles" / "planner.md"
        target.write_text("DIVERGENT", encoding="utf-8")
        with patch("cli.role.KISO_DIR", role_dir), \
             patch("cli.roles.KISO_DIR", role_dir):
            roles_reset(argparse.Namespace(name="planner", all=False, yes=True))
        assert target.read_text() == _package_roles()["planner"]


class TestCLIIntegration:
    def test_kiso_roles_subcommand_list(self):
        from cli import build_parser
        args = build_parser().parse_args(["roles", "list"])
        assert args.command == "roles"
        assert args.roles_command == "list"

    def test_kiso_roles_show(self):
        from cli import build_parser
        args = build_parser().parse_args(["roles", "show", "planner"])
        assert args.command == "roles"
        assert args.roles_command == "show"
        assert args.name == "planner"

    def test_kiso_roles_diff(self):
        from cli import build_parser
        args = build_parser().parse_args(["roles", "diff", "planner"])
        assert args.command == "roles"
        assert args.roles_command == "diff"
        assert args.name == "planner"

    def test_kiso_roles_reset(self):
        from cli import build_parser
        args = build_parser().parse_args(["roles", "reset", "planner", "--yes"])
        assert args.command == "roles"
        assert args.roles_command == "reset"
        assert args.name == "planner"

    def test_singular_role_alias_still_works(self):
        """`kiso role reset` (singular) keeps working for one cycle."""
        from cli import build_parser
        args = build_parser().parse_args(["role", "reset", "planner"])
        assert args.command == "role"
        assert args.role_command == "reset"
