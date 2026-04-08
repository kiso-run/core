"""CLI tests for project management commands (cli/project.py)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._cli_test_helpers import make_cli_args, mock_cli_config, mock_http_response


# ── project_list ──────────────────────────────────────────────


class TestProjectList:
    def test_empty(self, capsys):
        from cli.project import project_list

        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"projects": []}):
            project_list(args)
        assert "No projects found" in capsys.readouterr().out

    def test_multiple_projects(self, capsys):
        from cli.project import project_list

        projects = [
            {"id": 1, "name": "alpha", "description": "First project"},
            {"id": 2, "name": "beta", "description": ""},
        ]
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"projects": projects}):
            project_list(args)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "alpha" in out
        assert "First project" in out
        assert "[2]" in out
        assert "beta" in out

    def test_description_display(self, capsys):
        from cli.project import project_list

        projects = [{"id": 1, "name": "proj", "description": "Has desc"}]
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"projects": projects}):
            project_list(args)
        out = capsys.readouterr().out
        assert "Has desc" in out

    def test_no_description(self, capsys):
        from cli.project import project_list

        projects = [{"id": 3, "name": "nodesc"}]
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"projects": projects}):
            project_list(args)
        out = capsys.readouterr().out
        assert "nodesc" in out
        assert "—" not in out


# ── project_create ────────────────────────────────────────────


class TestProjectCreate:
    def test_happy_path(self, capsys):
        from cli.project import project_create

        args = make_cli_args(name="my-project")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"id": 5, "name": "my-project"}):
            project_create(args)
        out = capsys.readouterr().out
        assert "my-project" in out
        assert "id=5" in out

    def test_with_description(self, capsys):
        from cli.project import project_create

        args = make_cli_args(name="proj", description="A test project")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"id": 1, "name": "proj"}):
            project_create(args)
        assert "proj" in capsys.readouterr().out

    def test_require_admin(self):
        from cli.project import project_create

        args = make_cli_args(name="proj")
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            project_create(args)


# ── project_show ──────────────────────────────────────────────


class TestProjectShow:
    def test_full_details(self, capsys):
        from cli.project import project_show

        data = {
            "project": {
                "name": "demo",
                "description": "Demo project",
                "created_by": "alice",
            },
            "members": [
                {"username": "alice", "role": "owner"},
                {"username": "bob", "role": "member"},
            ],
        }
        args = make_cli_args(name="demo")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response(data):
            project_show(args)
        out = capsys.readouterr().out
        assert "demo" in out
        assert "Demo project" in out
        assert "alice" in out
        assert "owner" in out
        assert "bob" in out
        assert "Members (2)" in out

    def test_no_description(self, capsys):
        from cli.project import project_show

        data = {
            "project": {"name": "bare", "created_by": "root"},
            "members": [],
        }
        args = make_cli_args(name="bare")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response(data):
            project_show(args)
        out = capsys.readouterr().out
        assert "bare" in out
        assert "Description" not in out


# ── project_bind / project_unbind ─────────────────────────────


class TestProjectBind:
    def test_bind_success(self, capsys):
        from cli.project import project_bind

        args = make_cli_args(project="alpha", session="sess1")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"bound": True}):
            project_bind(args)
        out = capsys.readouterr().out
        assert "sess1" in out
        assert "alpha" in out
        assert "bound" in out.lower()

    def test_bind_require_admin(self):
        from cli.project import project_bind

        args = make_cli_args(project="alpha", session="s")
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            project_bind(args)


class TestProjectUnbind:
    def test_unbind_success(self, capsys):
        from cli.project import project_unbind

        args = make_cli_args(project="alpha", session="sess1")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"unbound": True}):
            project_unbind(args)
        out = capsys.readouterr().out
        assert "sess1" in out
        assert "unbound" in out.lower()

    def test_unbind_require_admin(self):
        from cli.project import project_unbind

        args = make_cli_args(project="alpha", session="s")
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            project_unbind(args)


# ── project_add_member ────────────────────────────────────────


class TestProjectAddMember:
    def test_add_default_role(self, capsys):
        from cli.project import project_add_member

        args = make_cli_args(project="proj", username="bob", role=None)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"added": True, "username": "bob", "role": "member"}):
            project_add_member(args)
        out = capsys.readouterr().out
        assert "bob" in out
        assert "member" in out

    def test_add_custom_role(self, capsys):
        from cli.project import project_add_member

        args = make_cli_args(project="proj", username="alice", role="admin")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"added": True, "username": "alice", "role": "admin"}):
            project_add_member(args)
        out = capsys.readouterr().out
        assert "alice" in out
        assert "admin" in out

    def test_add_require_admin(self):
        from cli.project import project_add_member

        args = make_cli_args(project="proj", username="bob", role=None)
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            project_add_member(args)


# ── project_remove_member ─────────────────────────────────────


class TestProjectRemoveMember:
    def test_remove_success(self, capsys):
        from cli.project import project_remove_member

        args = make_cli_args(project="proj", username="bob")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"removed": True}):
            project_remove_member(args)
        out = capsys.readouterr().out
        assert "bob" in out
        assert "Removed" in out

    def test_remove_require_admin(self):
        from cli.project import project_remove_member

        args = make_cli_args(project="proj", username="bob")
        with patch("cli.plugin_ops.require_admin", side_effect=SystemExit(1)), \
             pytest.raises(SystemExit):
            project_remove_member(args)


# ── project_members ───────────────────────────────────────────


class TestProjectMembers:
    def test_empty(self, capsys):
        from cli.project import project_members

        args = make_cli_args(project="proj")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"members": []}):
            project_members(args)
        assert "No members found" in capsys.readouterr().out

    def test_multiple_members(self, capsys):
        from cli.project import project_members

        members = [
            {"username": "alice", "role": "owner"},
            {"username": "bob", "role": "member"},
        ]
        args = make_cli_args(project="proj")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"members": members}):
            project_members(args)
        out = capsys.readouterr().out
        assert "alice" in out
        assert "owner" in out
        assert "bob" in out
        assert "member" in out
