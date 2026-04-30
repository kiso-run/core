"""Tests for ``cli/mcp.py`` subcommand handlers.

The CLI wraps config file IO so the tests mostly exercise the
happy path through ``_cmd_list``, ``_cmd_add``, ``_cmd_remove``,
``_cmd_env`` and confirm the file side-effects land in the right
place with the right permissions. ``_cmd_install`` is exercised
in dry-run mode to skip the real git/pip invocations.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from cli import mcp as cli_mcp


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    """Provide a valid minimal config.toml in tmp_path and point the
    CLI module's CONFIG_PATH at it."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[tokens]\ncli = "tok"\n\n'
        '[providers.openrouter]\nbase_url = "https://example.com/v1"\n\n'
        '[users.admin]\nrole = "admin"\n\n'
        '[models]\n'
        'planner = "x"\nreviewer = "x"\nmessenger = "x"\nbriefer = "x"\n'
        'classifier = "x"\ncurator = "x"\ntext = "x"\n\n'
        '[settings]\n'
        'max_plans = 3\nmax_replans = 5\nmax_planner_retries = 6\n'
        'max_review_retries = 3\nmax_worker_retries = 3\n'
        'max_stored_messages = 1000\nmax_output_size = 65536\n'
        'max_llm_calls_per_plan = 20\nllm_timeout = 180\n'
        'llm_cost_limit = 1.0\nsession_timeout = 3600\n'
        'message_max_length = 4000\nworker_idle_timeout = 60\n'
        'http_port = 8334\nhttp_listen = "127.0.0.1"\nhttp_bind = "*"\n'
        'sandbox_user = "nobody"\ninstall_timeout = 600\n'
        'kiso_dir_size_limit = 1000000000\nstuck_intervention_depth = 2\n'
        'briefer_enabled = false\n'
        'briefer_wrapper_filter_threshold = 10\n'
        'briefer_mcp_method_filter_threshold = 10\n'
        'webhook_allow_list = []\nwebhook_require_https = true\n'
        'webhook_secret = ""\nwebhook_max_payload = 1048576\n'
    )
    monkeypatch.setattr(cli_mcp, "CONFIG_PATH", path)
    monkeypatch.setattr(cli_mcp, "KISO_DIR", tmp_path)
    monkeypatch.setattr(cli_mcp, "MCP_ENV_DIR", tmp_path / "mcp")
    monkeypatch.setattr(cli_mcp, "MCP_LOG_DIR", tmp_path / "mcp")
    return path


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class TestList:
    def test_empty_list(self, cfg_path, capsys):
        assert cli_mcp._cmd_list() == 0
        captured = capsys.readouterr()
        assert "(no MCP servers configured)" in captured.out

    def test_populated_list(self, cfg_path, capsys):
        cli_mcp._cmd_add(
            _ns(
                name="github",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                cwd=None,
                env=[],
                url=None,
                header=[],
                timeout_s=60.0,
            )
        )
        assert cli_mcp._cmd_list() == 0
        captured = capsys.readouterr()
        assert "github" in captured.out
        assert "stdio" in captured.out
        assert "npx" in captured.out


class TestAdd:
    def test_add_stdio(self, cfg_path):
        rc = cli_mcp._cmd_add(
            _ns(
                name="s1",
                transport="stdio",
                command="npx",
                args=["-y", "@scope/pkg"],
                cwd=None,
                env=["FOO=bar"],
                url=None,
                header=[],
                timeout_s=30.0,
            )
        )
        assert rc == 0
        _, raw = cli_mcp._read_config_raw(cfg_path)
        entry = raw["mcp"]["s1"]
        assert entry["transport"] == "stdio"
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "@scope/pkg"]
        assert entry["env"] == {"FOO": "bar"}
        assert entry["timeout_s"] == 30.0

    def test_add_http(self, cfg_path):
        rc = cli_mcp._cmd_add(
            _ns(
                name="maps",
                transport="http",
                command=None,
                args=[],
                cwd=None,
                env=[],
                url="https://mapstools.googleapis.com/mcp",
                header=["X-Goog-Api-Key=fake"],
                timeout_s=60.0,
            )
        )
        assert rc == 0
        _, raw = cli_mcp._read_config_raw(cfg_path)
        entry = raw["mcp"]["maps"]
        assert entry["transport"] == "http"
        assert entry["url"] == "https://mapstools.googleapis.com/mcp"
        assert entry["headers"] == {"X-Goog-Api-Key": "fake"}

    def test_add_rejects_kiso_env(self, cfg_path):
        with pytest.raises(SystemExit):
            cli_mcp._cmd_add(
                _ns(
                    name="bad",
                    transport="stdio",
                    command="foo",
                    args=[],
                    cwd=None,
                    env=["KISO_SECRET=stolen"],
                    url=None,
                    header=[],
                    timeout_s=60.0,
                )
            )

    def test_add_rejects_invalid_name(self, cfg_path):
        with pytest.raises(SystemExit):
            cli_mcp._cmd_add(
                _ns(
                    name="Bad-Name",
                    transport="stdio",
                    command="foo",
                    args=[],
                    cwd=None,
                    env=[],
                    url=None,
                    header=[],
                    timeout_s=60.0,
                )
            )


class TestRemove:
    def test_remove_yes(self, cfg_path):
        cli_mcp._cmd_add(
            _ns(
                name="x",
                transport="stdio",
                command="foo",
                args=[],
                cwd=None,
                env=[],
                url=None,
                header=[],
                timeout_s=60.0,
            )
        )
        rc = cli_mcp._cmd_remove(_ns(name="x", yes=True))
        assert rc == 0
        _, raw = cli_mcp._read_config_raw(cfg_path)
        assert "mcp" not in raw or "x" not in raw.get("mcp", {})

    def test_remove_unknown(self, cfg_path):
        with pytest.raises(SystemExit):
            cli_mcp._cmd_remove(_ns(name="does-not-exist", yes=True))


class TestInstallDryRun:
    def test_dry_run_npm(self, cfg_path, capsys):
        rc = cli_mcp._cmd_install(
            _ns(
                from_url="npm:@modelcontextprotocol/server-github",
                name=None,
                dry_run=True,
            )
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "npx" in captured.out
        assert "@modelcontextprotocol/server-github" in captured.out
        assert "dry run" in captured.out


class TestInstallChatApprovalRecordsTrust:
    """M1604 — chat-approved untrusted install must auto-record the source
    in `~/.kiso/trust.json` so subsequent installs of the same source
    resolve to `tier=custom` instead of re-prompting.

    Each test patches `kiso.trust_store.TRUST_PATH` to an isolated tmp
    file, replaces `resolve_from_url` with a minimal `ResolvedServer`
    (empty `pre_install` so no subprocess fires), and stubs
    `_persist_server_entry` so the install side-effects don't touch the
    test config. The behaviour under test is purely:
    *did `add_prefix` fire (and exactly once) on the right branch?*
    """

    UNTRUSTED_URL = "https://github.com/random-org/cool-mcp"
    UNTRUSTED_KEY = "github.com/random-org/cool-mcp"
    TIER1_URL = "npm:@modelcontextprotocol/server-foo"
    TIER1_KEY = "npm:@modelcontextprotocol/server-foo"

    @staticmethod
    def _patch_trust_path(monkeypatch, tmp_path):
        from kiso import trust_store
        path = tmp_path / "trust.json"
        monkeypatch.setattr(trust_store, "TRUST_PATH", path)
        return path

    @staticmethod
    def _patch_install_side_effects(monkeypatch):
        from kiso.mcp.install import ResolvedServer

        def _fake_resolve(from_url, name_hint=None):
            return ResolvedServer(
                name="fake-mcp",
                transport="stdio",
                command="echo",
                args=["fake"],
            )

        monkeypatch.setattr(cli_mcp, "resolve_from_url", _fake_resolve)
        monkeypatch.setattr(cli_mcp, "_persist_server_entry", lambda *a, **k: 0)
        monkeypatch.setattr(cli_mcp, "check_runtime_dependencies", lambda: [])

    def _trust_store_mcp(self):
        from kiso.trust_store import load_trust_store
        return list(load_trust_store().mcp)

    def test_untrusted_with_yes_flag_records_prefix(
        self, cfg_path, monkeypatch, capsys,
    ):
        trust_path = self._patch_trust_path(monkeypatch, cfg_path.parent)
        self._patch_install_side_effects(monkeypatch)

        rc = cli_mcp._cmd_install(
            _ns(
                from_url=self.UNTRUSTED_URL,
                name=None,
                dry_run=False,
                yes=True,
            )
        )
        assert rc == 0
        assert trust_path.exists(), "trust.json must be written after approval"
        assert self.UNTRUSTED_KEY in self._trust_store_mcp(), (
            f"--yes approval did not record {self.UNTRUSTED_KEY!r}"
        )

    def test_untrusted_interactive_yes_records_prefix(
        self, cfg_path, monkeypatch, capsys,
    ):
        trust_path = self._patch_trust_path(monkeypatch, cfg_path.parent)
        self._patch_install_side_effects(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "y")

        rc = cli_mcp._cmd_install(
            _ns(
                from_url=self.UNTRUSTED_URL,
                name=None,
                dry_run=False,
                yes=False,
            )
        )
        assert rc == 0
        assert self.UNTRUSTED_KEY in self._trust_store_mcp()

    def test_untrusted_interactive_no_does_not_record_prefix(
        self, cfg_path, monkeypatch, capsys,
    ):
        trust_path = self._patch_trust_path(monkeypatch, cfg_path.parent)
        self._patch_install_side_effects(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")

        rc = cli_mcp._cmd_install(
            _ns(
                from_url=self.UNTRUSTED_URL,
                name=None,
                dry_run=False,
                yes=False,
            )
        )
        assert rc != 0, "rejection must abort install with non-zero exit"
        assert not trust_path.exists() or self.UNTRUSTED_KEY not in self._trust_store_mcp(), (
            "rejection must NOT record the prefix"
        )

    def test_tier1_install_does_not_touch_trust_store(
        self, cfg_path, monkeypatch, capsys,
    ):
        trust_path = self._patch_trust_path(monkeypatch, cfg_path.parent)
        self._patch_install_side_effects(monkeypatch)

        rc = cli_mcp._cmd_install(
            _ns(
                from_url=self.TIER1_URL,
                name=None,
                dry_run=False,
                yes=True,
            )
        )
        assert rc == 0
        assert not trust_path.exists() or self._trust_store_mcp() == [], (
            "tier1 install must not write to the user trust store"
        )

    def test_custom_install_does_not_double_add(
        self, cfg_path, monkeypatch, capsys,
    ):
        # Pre-seed the trust store so the source resolves as tier=custom.
        from kiso.trust_store import save_trust_store, TrustStore
        trust_path = self._patch_trust_path(monkeypatch, cfg_path.parent)
        save_trust_store(TrustStore(mcp=[self.UNTRUSTED_KEY], skill=[]))
        self._patch_install_side_effects(monkeypatch)

        rc = cli_mcp._cmd_install(
            _ns(
                from_url=self.UNTRUSTED_URL,
                name=None,
                dry_run=False,
                yes=False,  # no prompt expected — already trusted
            )
        )
        assert rc == 0
        prefixes = self._trust_store_mcp()
        assert prefixes.count(self.UNTRUSTED_KEY) == 1, (
            f"already-custom source must not be added twice; got {prefixes}"
        )


class TestEnv:
    def test_env_set_creates_file_with_600_perms(self, cfg_path, tmp_path):
        rc = cli_mcp._cmd_env(
            _ns(
                mcp_env_command="set",
                name="github",
                key="GITHUB_TOKEN",
                value="ghp_abc",
            )
        )
        assert rc == 0
        path = tmp_path / "mcp" / "github.env"
        assert path.exists()
        mode = path.stat().st_mode
        assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0
        content = path.read_text()
        assert "GITHUB_TOKEN=ghp_abc" in content

    def test_env_set_rejects_kiso_keys(self, cfg_path):
        with pytest.raises(SystemExit):
            cli_mcp._cmd_env(
                _ns(
                    mcp_env_command="set",
                    name="github",
                    key="KISO_SECRET",
                    value="stolen",
                )
            )

    def test_env_unset(self, cfg_path, tmp_path):
        cli_mcp._cmd_env(
            _ns(
                mcp_env_command="set",
                name="github",
                key="GITHUB_TOKEN",
                value="ghp_abc",
            )
        )
        rc = cli_mcp._cmd_env(
            _ns(mcp_env_command="unset", name="github", key="GITHUB_TOKEN")
        )
        assert rc == 0
        path = tmp_path / "mcp" / "github.env"
        assert "GITHUB_TOKEN" not in path.read_text()

    def test_env_list_shows_keys_only(self, cfg_path, capsys):
        cli_mcp._cmd_env(
            _ns(
                mcp_env_command="set",
                name="github",
                key="GITHUB_TOKEN",
                value="secret",
            )
        )
        rc = cli_mcp._cmd_env(_ns(mcp_env_command="list", name="github"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN" in captured.out
        assert "secret" not in captured.out

    def test_env_show_prints_values_with_warning(self, cfg_path, capsys):
        cli_mcp._cmd_env(
            _ns(
                mcp_env_command="set",
                name="github",
                key="GITHUB_TOKEN",
                value="secret",
            )
        )
        rc = cli_mcp._cmd_env(_ns(mcp_env_command="show", name="github"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "GITHUB_TOKEN=secret" in captured.out
        assert "WARNING" in captured.err
