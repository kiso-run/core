"""M1542 — load-time allowlist validation.

When the daemon boots, any entry in
``[users.<name>.mcp].allow`` / ``[users.<name>.skills].allow`` that
does not match a currently-installed MCP method or skill name must
produce a WARNING (not a hard error — a user may have legitimately
removed an MCP server since the config was written). The warning
names the user, the key, and the offending entry so the user can
fix the typo.

The validator is pure: it takes a config + catalogs and returns the
list of offending ``(user, key, unknown)`` triples. Wiring into the
boot path is a separate integration concern.
"""

from __future__ import annotations

import logging

import pytest

from kiso.config import Config, Provider, User


def _base_config(users: dict[str, User]) -> Config:
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://x")},
        users=users,
        models={},
        settings={},
        raw={},
    )


class TestValidateUserAllowlists:

    def test_empty_users_returns_empty(self) -> None:
        from kiso.config_checks import validate_user_allowlists

        result = validate_user_allowlists(
            _base_config({}),
            mcp_methods=["filesystem.read", "filesystem.write"],
            skill_names=["code-review"],
        )
        assert result == []

    def test_all_known_returns_empty(self) -> None:
        from kiso.config_checks import validate_user_allowlists

        users = {
            "anna": User(
                role="user",
                mcp=["filesystem.read", "filesystem.write"],
                skills=["code-review"],
            ),
        }
        result = validate_user_allowlists(
            _base_config(users),
            mcp_methods=["filesystem.read", "filesystem.write"],
            skill_names=["code-review"],
        )
        assert result == []

    def test_unknown_mcp_entry_detected(self) -> None:
        from kiso.config_checks import validate_user_allowlists

        users = {
            "anna": User(
                role="user",
                mcp=["filesystem.read", "typo.method"],
            ),
        }
        result = validate_user_allowlists(
            _base_config(users),
            mcp_methods=["filesystem.read"],
            skill_names=[],
        )
        assert result == [("anna", "mcp", "typo.method")]

    def test_unknown_skill_entry_detected(self) -> None:
        from kiso.config_checks import validate_user_allowlists

        users = {
            "anna": User(
                role="user",
                skills=["code-review", "made-up-skill"],
            ),
        }
        result = validate_user_allowlists(
            _base_config(users),
            mcp_methods=[],
            skill_names=["code-review"],
        )
        assert result == [("anna", "skills", "made-up-skill")]

    def test_star_wildcard_is_valid(self) -> None:
        from kiso.config_checks import validate_user_allowlists

        users = {
            "anna": User(role="user", mcp="*", skills="*"),
        }
        result = validate_user_allowlists(
            _base_config(users),
            mcp_methods=["x.y"],
            skill_names=["z"],
        )
        assert result == []

    def test_none_allowlist_is_valid(self) -> None:
        from kiso.config_checks import validate_user_allowlists

        users = {"anna": User(role="user", mcp=None, skills=None)}
        result = validate_user_allowlists(
            _base_config(users),
            mcp_methods=[],
            skill_names=[],
        )
        assert result == []

    def test_glob_prefix_accepted(self) -> None:
        """``filesystem.*`` matches any filesystem.* method."""
        from kiso.config_checks import validate_user_allowlists

        users = {
            "anna": User(role="user", mcp=["filesystem.*", "missing.*"]),
        }
        result = validate_user_allowlists(
            _base_config(users),
            mcp_methods=["filesystem.read", "filesystem.write"],
            skill_names=[],
        )
        # filesystem.* matches; missing.* matches nothing.
        assert result == [("anna", "mcp", "missing.*")]


class TestEmitWarnings:
    def test_logs_one_warning_per_offender(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiso.config_checks import (
            validate_user_allowlists,
            emit_allowlist_warnings,
        )

        users = {
            "anna": User(role="user", mcp=["typo.a", "typo.b"]),
            "luca": User(role="user", skills=["made-up"]),
        }
        issues = validate_user_allowlists(
            _base_config(users),
            mcp_methods=[],
            skill_names=[],
        )
        assert len(issues) == 3

        caplog.set_level(logging.WARNING, logger="kiso.config_checks")
        emit_allowlist_warnings(issues)

        rendered = "\n".join(r.message for r in caplog.records)
        assert rendered.count("anna") == 2
        assert rendered.count("luca") == 1
        assert "typo.a" in rendered
        assert "typo.b" in rendered
        assert "made-up" in rendered
