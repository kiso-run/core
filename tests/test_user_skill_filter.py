"""Tests for per-user skill + MCP allowlist (migrating away from wrappers).

Business requirement: ``[users.<name>]`` allows per-user allowlists
for both MCP methods and skills. The retired ``wrappers`` field is
rejected with a migration hint. Admin / ``"*"`` see everything;
a role=user without an explicit list defaults to deny. Unknown
names in an allowlist raise at config-load time, not at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.config import User, load_config


_CONFIG_HEAD = """
[tokens]
cli = "t"

[providers.openrouter]
base_url = "https://example.com/v1"

[models]
planner = "m"
reviewer = "m"
messenger = "m"
briefer = "m"
classifier = "m"
curator = "m"
worker = "m"
summarizer = "m"
paraphraser = "m"
consolidator = "m"

"""


def _write_config(tmp_path: Path, users_block: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_CONFIG_HEAD + users_block)
    return p


class TestUserDataclass:
    def test_user_has_skills_field(self):
        u = User(role="user", mcp=["x:y"], skills=["python-debug"])
        assert u.skills == ["python-debug"]
        assert u.mcp == ["x:y"]

    def test_user_no_longer_has_wrappers(self):
        with pytest.raises(TypeError):
            User(role="user", wrappers=["x"])  # type: ignore[call-arg]


class TestConfigParseSkills:
    def test_skills_star_parsed(self, tmp_path, monkeypatch):
        p = _write_config(
            tmp_path,
            '[users.alice]\nrole = "admin"\n'
            '[users.bob]\nrole = "user"\nmcp = "*"\nskills = "*"\n',
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk")
        monkeypatch.setattr("kiso.config.CONFIG_PATH", p)
        cfg = load_config()
        assert cfg.users["bob"].skills == "*"
        assert cfg.users["bob"].mcp == "*"

    def test_skills_list_parsed(self, tmp_path, monkeypatch):
        p = _write_config(
            tmp_path,
            '[users.alice]\nrole = "admin"\n'
            '[users.bob]\nrole = "user"\nmcp = []\n'
            'skills = ["python-debug", "writing-style"]\n',
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk")
        monkeypatch.setattr("kiso.config.CONFIG_PATH", p)
        cfg = load_config()
        assert cfg.users["bob"].skills == ["python-debug", "writing-style"]

    def test_skills_absent_defaults_to_none(self, tmp_path, monkeypatch):
        p = _write_config(
            tmp_path,
            '[users.alice]\nrole = "admin"\n'
            '[users.bob]\nrole = "user"\nmcp = []\n',
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk")
        monkeypatch.setattr("kiso.config.CONFIG_PATH", p)
        cfg = load_config()
        assert cfg.users["bob"].skills is None


class TestConfigRejectsWrappers:
    def test_wrappers_field_rejected_with_migration_hint(
        self, tmp_path, monkeypatch, capsys
    ):
        p = _write_config(
            tmp_path,
            '[users.alice]\nrole = "admin"\n'
            '[users.bob]\nrole = "user"\nwrappers = ["browser"]\n',
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk")
        monkeypatch.setattr("kiso.config.CONFIG_PATH", p)
        with pytest.raises(SystemExit):
            load_config()
        err = capsys.readouterr().err
        assert "wrappers" in err
        assert "v0.10" in err or "removed" in err.lower()
        assert "mcp" in err.lower() or "skills" in err.lower()


class TestAllowlistFilter:
    """Filtering contract exposed via kiso.brain.common.filter_skills_by_user.

    Parallel to filter_mcp_catalog_by_user (M1539). Given the full
    list of discovered skills and a user's allowlist, returns only
    the skills the user is permitted to see.
    """

    def test_admin_sees_all(self):
        from kiso.brain.common import filter_skills_by_user
        result = filter_skills_by_user(
            ["a", "b", "c"], role="admin", allowlist=None
        )
        assert result == ["a", "b", "c"]

    def test_star_sees_all(self):
        from kiso.brain.common import filter_skills_by_user
        result = filter_skills_by_user(
            ["a", "b", "c"], role="user", allowlist="*"
        )
        assert result == ["a", "b", "c"]

    def test_list_limits_to_allowlist(self):
        from kiso.brain.common import filter_skills_by_user
        result = filter_skills_by_user(
            ["a", "b", "c"], role="user", allowlist=["a", "c"]
        )
        assert result == ["a", "c"]

    def test_absent_allowlist_for_role_user_denies(self):
        from kiso.brain.common import filter_skills_by_user
        result = filter_skills_by_user(
            ["a", "b", "c"], role="user", allowlist=None
        )
        assert result == []

    def test_absent_allowlist_for_admin_allows(self):
        from kiso.brain.common import filter_skills_by_user
        result = filter_skills_by_user(
            ["a", "b", "c"], role="admin", allowlist=None
        )
        assert result == ["a", "b", "c"]
