"""Tests for kiso.cli_skill — skill management CLI commands."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiso.cli import build_parser
from kiso.cli_skill import _is_url, run_skill_command, url_to_name
from kiso.config import User


# ── Helpers ──────────────────────────────────────────────────


def _admin_cfg():
    cfg = MagicMock()
    cfg.users = {"alice": User(role="admin")}
    return cfg


@pytest.fixture()
def mock_admin():
    """Patch load_config and getpass so _require_admin passes."""
    with (
        patch("kiso.cli_skill.load_config", return_value=_admin_cfg()),
        patch("kiso.cli_skill.getpass.getuser", return_value="alice"),
    ):
        yield


# ── url_to_name ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "git@github.com:sniku/jQuery-doubleScroll.git",
            "github-com_sniku_jquery-doublescroll",
        ),
        (
            "https://gitlab.com/team/cool-skill.git",
            "gitlab-com_team_cool-skill",
        ),
        (
            "https://github.com/someone/my-skill",
            "github-com_someone_my-skill",
        ),
        (
            "git@GitHub.COM:Team/Mixed-Case.git",
            "github-com_team_mixed-case",
        ),
        (
            "https://example.com/org/sub/deep-repo.git",
            "example-com_org_sub_deep-repo",
        ),
        (
            "git@github.com:user/repo",
            "github-com_user_repo",
        ),
    ],
)
def test_url_to_name(url: str, expected: str):
    assert url_to_name(url) == expected


# ── _is_url ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target, expected",
    [
        ("git@github.com:user/repo.git", True),
        ("https://github.com/user/repo.git", True),
        ("http://example.com/repo.git", True),
        ("search", False),
    ],
)
def test_is_url(target: str, expected: bool):
    assert _is_url(target) == expected


# ── Subparser parsing ────────────────────────────────────────


def test_parse_skill_list():
    parser = build_parser()
    args = parser.parse_args(["skill", "list"])
    assert args.command == "skill"
    assert args.skill_command == "list"


def test_parse_skill_search_no_query():
    parser = build_parser()
    args = parser.parse_args(["skill", "search"])
    assert args.skill_command == "search"
    assert args.query == ""


def test_parse_skill_search_with_query():
    parser = build_parser()
    args = parser.parse_args(["skill", "search", "web"])
    assert args.skill_command == "search"
    assert args.query == "web"


def test_parse_skill_install():
    parser = build_parser()
    args = parser.parse_args(["skill", "install", "search"])
    assert args.skill_command == "install"
    assert args.target == "search"
    assert args.name is None
    assert args.no_deps is False
    assert args.show_deps is False


def test_parse_skill_install_url_with_name():
    parser = build_parser()
    args = parser.parse_args([
        "skill", "install", "git@github.com:user/repo.git", "--name", "foo",
    ])
    assert args.target == "git@github.com:user/repo.git"
    assert args.name == "foo"


def test_parse_skill_install_no_deps():
    parser = build_parser()
    args = parser.parse_args(["skill", "install", "search", "--no-deps"])
    assert args.no_deps is True


def test_parse_skill_install_show_deps():
    parser = build_parser()
    args = parser.parse_args(["skill", "install", "search", "--show-deps"])
    assert args.show_deps is True


def test_parse_skill_update():
    parser = build_parser()
    args = parser.parse_args(["skill", "update", "search"])
    assert args.skill_command == "update"
    assert args.target == "search"


def test_parse_skill_update_all():
    parser = build_parser()
    args = parser.parse_args(["skill", "update", "all"])
    assert args.target == "all"


def test_parse_skill_remove():
    parser = build_parser()
    args = parser.parse_args(["skill", "remove", "search"])
    assert args.skill_command == "remove"
    assert args.name == "search"


def test_parse_skill_no_subcommand():
    parser = build_parser()
    args = parser.parse_args(["skill"])
    assert args.command == "skill"
    assert args.skill_command is None


# ── run_skill_command dispatcher ─────────────────────────────


def test_run_skill_command_no_subcommand(capsys):
    args = argparse.Namespace(skill_command=None)
    with pytest.raises(SystemExit, match="1"):
        run_skill_command(args)
    out = capsys.readouterr().out
    assert "usage:" in out


# ── _require_admin ───────────────────────────────────────────


def test_require_admin_passes():
    from kiso.cli_skill import _require_admin

    cfg = MagicMock()
    cfg.users = {"alice": User(role="admin")}
    with (
        patch("kiso.cli_skill.load_config", return_value=cfg),
        patch("kiso.cli_skill.getpass.getuser", return_value="alice"),
    ):
        _require_admin()  # should not raise


def test_require_admin_non_admin_exits(capsys):
    from kiso.cli_skill import _require_admin

    cfg = MagicMock()
    cfg.users = {"bob": User(role="user", skills="*")}
    with (
        patch("kiso.cli_skill.load_config", return_value=cfg),
        patch("kiso.cli_skill.getpass.getuser", return_value="bob"),
        pytest.raises(SystemExit, match="1"),
    ):
        _require_admin()
    out = capsys.readouterr().out
    assert "not an admin" in out


def test_require_admin_unknown_user_exits(capsys):
    from kiso.cli_skill import _require_admin

    cfg = MagicMock()
    cfg.users = {"alice": User(role="admin")}
    with (
        patch("kiso.cli_skill.load_config", return_value=cfg),
        patch("kiso.cli_skill.getpass.getuser", return_value="unknown"),
        pytest.raises(SystemExit, match="1"),
    ):
        _require_admin()
    out = capsys.readouterr().out
    assert "unknown user" in out


# ── _skill_list ──────────────────────────────────────────────


def test_skill_list_empty(capsys):
    from kiso.cli_skill import _skill_list

    with patch("kiso.cli_skill.discover_skills", return_value=[]):
        _skill_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "No skills installed." in out


def test_skill_list_shows_skills(capsys):
    from kiso.cli_skill import _skill_list

    skills = [
        {"name": "search", "version": "0.1.0", "summary": "Web search"},
        {"name": "aider", "version": "0.3.2", "summary": "Code editing"},
    ]
    with patch("kiso.cli_skill.discover_skills", return_value=skills):
        _skill_list(argparse.Namespace())
    out = capsys.readouterr().out
    assert "search" in out
    assert "0.1.0" in out
    assert "Web search" in out
    assert "aider" in out
    assert "0.3.2" in out


def test_skill_list_column_alignment(capsys):
    from kiso.cli_skill import _skill_list

    skills = [
        {"name": "a", "version": "1.0", "summary": "Short"},
        {"name": "longname", "version": "10.20.30", "summary": "Long"},
    ]
    with patch("kiso.cli_skill.discover_skills", return_value=skills):
        _skill_list(argparse.Namespace())
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2
    # Both lines should have the dash separator at the same column
    # (names and versions are padded with ljust)
    parts0 = lines[0].split("—")
    parts1 = lines[1].split("—")
    assert len(parts0[0]) == len(parts1[0])


# ── _skill_search ────────────────────────────────────────────


def test_skill_search_no_query(capsys):
    from kiso.cli_skill import _skill_search

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "items": [
            {"name": "skill-search", "description": "Web search"},
            {"name": "skill-aider", "description": "Code editing"},
        ],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        _skill_search(argparse.Namespace(query=""))

    out = capsys.readouterr().out
    assert "search" in out
    assert "aider" in out
    # Verify skill- prefix was stripped
    assert "skill-search" not in out

    # Verify query params
    call_args = mock_get.call_args
    assert "org:kiso-run" in call_args[1]["params"]["q"]
    assert "topic:kiso-skill" in call_args[1]["params"]["q"]


def test_skill_search_with_query(capsys):
    from kiso.cli_skill import _skill_search

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "items": [{"name": "skill-search", "description": "Web search"}],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        _skill_search(argparse.Namespace(query="web"))

    call_args = mock_get.call_args
    assert "web" in call_args[1]["params"]["q"]


def test_skill_search_network_error(capsys):
    import httpx

    with (
        patch("httpx.get", side_effect=httpx.ConnectError("fail")),
        pytest.raises(SystemExit, match="1"),
    ):
        from kiso.cli_skill import _skill_search

        _skill_search(argparse.Namespace(query=""))
    out = capsys.readouterr().out
    assert "GitHub search failed" in out


def test_skill_search_no_results(capsys):
    from kiso.cli_skill import _skill_search

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": []}
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.get", return_value=mock_resp):
        _skill_search(argparse.Namespace(query="nonexistent"))
    out = capsys.readouterr().out
    assert "No skills found." in out


# ── _skill_install ───────────────────────────────────────────


def _fake_clone_with_manifest(name="search", summary="Web search"):
    """Return a fake_clone function that writes a valid skill repo."""
    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kiso.toml").write_text(
            f'[kiso]\ntype = "skill"\nname = "{name}"\n'
            f"[kiso.skill]\n"
            f'summary = "{summary}"\n'
        )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n")
        return subprocess.CompletedProcess(cmd, 0)
    return fake_clone


def _ok_run(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0)


def test_skill_install_official(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    clone_fn = _fake_clone_with_manifest()

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=run_dispatch),
        patch("kiso.cli_skill.check_deps", return_value=[]),
    ):
        args = argparse.Namespace(
            target="search", name=None, no_deps=False, show_deps=False,
        )
        _skill_install(args)

    out = capsys.readouterr().out
    assert "installed successfully" in out


def test_skill_install_unofficial_with_confirm(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    clone_fn = _fake_clone_with_manifest("myskill", "Custom skill")

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=run_dispatch),
        patch("kiso.cli_skill.check_deps", return_value=[]),
        patch("builtins.input", return_value="y"),
    ):
        args = argparse.Namespace(
            target="https://github.com/someone/myskill.git",
            name="myskill",
            no_deps=False,
            show_deps=False,
        )
        _skill_install(args)

    out = capsys.readouterr().out
    assert "unofficial" in out.lower()
    assert "installed successfully" in out


def test_skill_install_unofficial_declined(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    clone_fn = _fake_clone_with_manifest("myskill", "Custom skill")

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=run_dispatch),
        patch("builtins.input", return_value="n"),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="https://github.com/someone/myskill.git",
            name="myskill",
            no_deps=False,
            show_deps=False,
        )
        _skill_install(args)

    out = capsys.readouterr().out
    assert "cancelled" in out.lower()
    assert not (skills_dir / "myskill").exists()


def test_skill_install_custom_name(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    clone_fn = _fake_clone_with_manifest("myskill", "Custom")

    def run_dispatch(cmd, **kwargs):
        if cmd[0] == "git":
            return clone_fn(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=run_dispatch),
        patch("kiso.cli_skill.check_deps", return_value=[]),
        patch("builtins.input", return_value="y"),
    ):
        args = argparse.Namespace(
            target="https://github.com/someone/repo.git",
            name="custom",
            no_deps=False,
            show_deps=False,
        )
        _skill_install(args)

    assert (skills_dir / "custom").exists()


def test_skill_install_no_deps_flag(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    def clone_with_deps(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kiso.toml").write_text(
            '[kiso]\ntype = "skill"\nname = "search"\n'
            "[kiso.skill]\n"
            'summary = "Search"\n'
        )
        (dest / "run.py").write_text("pass\n")
        (dest / "pyproject.toml").write_text("[project]\nname = 'search'\n")
        (dest / "deps.sh").write_text("apt install something\n")
        return subprocess.CompletedProcess(cmd, 0)

    run_calls = []

    def tracking_run(cmd, **kwargs):
        run_calls.append(cmd)
        if cmd[0] == "git":
            return clone_with_deps(cmd, **kwargs)
        return _ok_run(cmd, **kwargs)

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=tracking_run),
        patch("kiso.cli_skill.check_deps", return_value=[]),
    ):
        args = argparse.Namespace(
            target="search", name=None, no_deps=True, show_deps=False,
        )
        _skill_install(args)

    # deps.sh should not have been called
    bash_calls = [c for c in run_calls if c[0] == "bash"]
    assert len(bash_calls) == 0


def test_skill_install_show_deps(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    deps_content = "#!/bin/bash\napt install ffmpeg\n"

    def fake_clone(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "deps.sh").write_text(deps_content)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_clone):
        args = argparse.Namespace(
            target="search", name=None, no_deps=False, show_deps=True,
        )
        _skill_install(args)

    out = capsys.readouterr().out
    assert "apt install ffmpeg" in out


def test_skill_install_already_installed(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "search").mkdir()

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="search", name=None, no_deps=False, show_deps=False,
        )
        _skill_install(args)

    out = capsys.readouterr().out
    assert "already installed" in out


def test_skill_install_git_clone_failure_cleanup(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_install

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    def fake_clone_fail(cmd, **kwargs):
        dest = Path(cmd[3])
        dest.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 1, stderr="fatal: repo not found")

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=fake_clone_fail),
        pytest.raises(SystemExit, match="1"),
    ):
        args = argparse.Namespace(
            target="search", name=None, no_deps=False, show_deps=False,
        )
        _skill_install(args)

    out = capsys.readouterr().out
    assert "git clone failed" in out
    assert not (skills_dir / "search").exists()


# ── _skill_update ────────────────────────────────────────────


def test_skill_update_single(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_update

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "search"
    skill_dir.mkdir()
    (skill_dir / "kiso.toml").write_text('[kiso]\ntype = "skill"\nname = "search"\n')

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=_ok_run),
        patch("kiso.cli_skill.check_deps", return_value=[]),
    ):
        _skill_update(argparse.Namespace(target="search"))

    out = capsys.readouterr().out
    assert "updated" in out


def test_skill_update_all(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_update

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in ["search", "aider"]:
        d = skills_dir / name
        d.mkdir()
        (d / "kiso.toml").write_text(f'[kiso]\ntype = "skill"\nname = "{name}"\n')

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=_ok_run),
        patch("kiso.cli_skill.check_deps", return_value=[]),
    ):
        _skill_update(argparse.Namespace(target="all"))

    out = capsys.readouterr().out
    assert "aider" in out and "updated" in out
    assert "search" in out


def test_skill_update_nonexistent(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_update

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _skill_update(argparse.Namespace(target="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out


def test_skill_update_git_pull_failure(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_update

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "search").mkdir()

    def fake_pull_fail(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stderr="error: cannot pull")

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        patch("subprocess.run", side_effect=fake_pull_fail),
        pytest.raises(SystemExit, match="1"),
    ):
        _skill_update(argparse.Namespace(target="search"))

    out = capsys.readouterr().out
    assert "git pull failed" in out


# ── _skill_remove ────────────────────────────────────────────


def test_skill_remove_existing(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_remove

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "search").mkdir()

    with patch("kiso.cli_skill.SKILLS_DIR", skills_dir):
        _skill_remove(argparse.Namespace(name="search"))

    out = capsys.readouterr().out
    assert "removed" in out
    assert not (skills_dir / "search").exists()


def test_skill_remove_nonexistent(tmp_path, mock_admin, capsys):
    from kiso.cli_skill import _skill_remove

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    with (
        patch("kiso.cli_skill.SKILLS_DIR", skills_dir),
        pytest.raises(SystemExit, match="1"),
    ):
        _skill_remove(argparse.Namespace(name="nonexistent"))

    out = capsys.readouterr().out
    assert "not installed" in out
