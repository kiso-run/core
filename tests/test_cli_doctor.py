"""Tests for ``kiso doctor`` — the unified health-check command.

Business requirement: one CLI entry point runs every health check
Kiso needs to self-diagnose (runtime deps on PATH, config shape,
LLM reachability, MCP pool, skills, sandbox posture, trust store,
SQLite DB, and workspace writability). The command emits a Rich
table grouped by category, accepts ``--json`` for CI consumption,
and exits 0 only when every check is green. Red rows carry a
targeted remediation suggestion so the operator can fix the issue
without combing through a dozen separate sub-commands.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.doctor import (
    CheckResult,
    DoctorContext,
    check_config,
    check_llm,
    check_mcp,
    check_runtime,
    check_sandbox,
    check_skills,
    check_store,
    check_trust,
    check_workspace,
    render_json,
    render_table,
    run_checks,
)
from kiso.config import Config, Provider
from tests.conftest import full_models, full_settings


def _ctx(tmp_path: Path, *, config: Config | None = None, api_key: str = "x") -> DoctorContext:
    if config is None:
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://example.com/v1")},
            users={},
            models=full_models(),
            settings=full_settings(),
            raw={},
        )
    return DoctorContext(
        kiso_dir=tmp_path,
        config=config,
        config_path=tmp_path / "config.toml",
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# CheckResult + driver
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_ok_status(self):
        r = CheckResult(category="Runtime", name="uv", status="ok")
        assert r.category == "Runtime"
        assert r.status == "ok"

    def test_rejects_invalid_status(self):
        with pytest.raises(ValueError):
            CheckResult(category="x", name="y", status="maybe")  # type: ignore[arg-type]


class TestRunChecks:
    def test_aggregates_results_from_every_category(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "tok")
        ctx = _ctx(tmp_path, api_key="tok")
        (tmp_path / "config.toml").write_text("[providers.openrouter]\nbase_url='x'\n")
        # Avoid network/subprocess in the aggregate driver by mocking the
        # network-touching checks. Other checks run against the tmp dir.
        with patch("cli.doctor.check_llm", return_value=[
            CheckResult(category="LLM", name="probe", status="ok")
        ]), patch("cli.doctor.check_mcp", return_value=[
            CheckResult(category="MCP", name="servers", status="ok")
        ]):
            results = run_checks(ctx)

        categories = {r.category for r in results}
        # Every advertised category appears at least once in the output.
        assert {
            "Runtime", "Config", "LLM", "MCP", "Skills",
            "Sandbox", "Trust", "Store", "Workspace",
        }.issubset(categories)

    def test_all_green_exits_zero(self, tmp_path, monkeypatch):
        """When every check returns ok, run_checks produces a result set
        that render_exit_code maps to 0."""
        from cli.doctor import exit_code_for_results

        ok_results = [
            CheckResult(category="Runtime", name="uv", status="ok"),
            CheckResult(category="Config", name="api_key", status="ok"),
        ]
        assert exit_code_for_results(ok_results) == 0

    def test_any_red_exits_nonzero(self):
        from cli.doctor import exit_code_for_results

        mixed = [
            CheckResult(category="Runtime", name="uv", status="ok"),
            CheckResult(category="Config", name="api_key", status="fail"),
        ]
        assert exit_code_for_results(mixed) != 0

    def test_warn_does_not_trip_exit_code(self):
        from cli.doctor import exit_code_for_results

        warned = [
            CheckResult(category="Runtime", name="npx", status="warn"),
            CheckResult(category="Config", name="api_key", status="ok"),
        ]
        assert exit_code_for_results(warned) == 0


# ---------------------------------------------------------------------------
# Per-category checks
# ---------------------------------------------------------------------------


class TestRuntimeChecks:
    def test_all_tools_on_path_green(self, tmp_path):
        ctx = _ctx(tmp_path)
        with patch("cli.doctor.shutil.which", return_value="/usr/bin/x"):
            results = check_runtime(ctx)
        by_name = {r.name: r for r in results}
        for tool in ("uv", "uvx", "npx", "git"):
            assert by_name[tool].status == "ok"

    def test_missing_uv_produces_fail_with_suggestion(self, tmp_path):
        ctx = _ctx(tmp_path)
        def _which(x):
            return None if x == "uv" else "/usr/bin/x"
        with patch("cli.doctor.shutil.which", side_effect=_which):
            results = check_runtime(ctx)
        uv = next(r for r in results if r.name == "uv")
        assert uv.status == "fail"
        assert "install" in uv.suggestion.lower() or "path" in uv.suggestion.lower()

    def test_kiso_dir_writable_check(self, tmp_path):
        ctx = _ctx(tmp_path)
        with patch("cli.doctor.shutil.which", return_value="/usr/bin/x"):
            results = check_runtime(ctx)
        workspace = next(r for r in results if r.name == "kiso_dir_writable")
        assert workspace.status == "ok"

    def test_kiso_dir_missing_produces_fail(self, tmp_path):
        ctx = _ctx(tmp_path / "does-not-exist")
        with patch("cli.doctor.shutil.which", return_value="/usr/bin/x"):
            results = check_runtime(ctx)
        workspace = next(r for r in results if r.name == "kiso_dir_writable")
        assert workspace.status == "fail"


class TestConfigChecks:
    def test_config_present_and_api_key_set_green(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "tok")
        (tmp_path / "config.toml").write_text("[providers.openrouter]\nbase_url='x'\n")
        ctx = _ctx(tmp_path, api_key="tok")
        results = check_config(ctx)
        by_name = {r.name: r for r in results}
        assert by_name["config_file"].status == "ok"
        assert by_name["openrouter_api_key"].status == "ok"

    def test_config_missing_produces_fail(self, tmp_path):
        ctx = _ctx(tmp_path, api_key="")
        # config_path in ctx points at missing file
        results = check_config(ctx)
        cf = next(r for r in results if r.name == "config_file")
        assert cf.status == "fail"

    def test_missing_api_key_produces_fail(self, tmp_path):
        (tmp_path / "config.toml").write_text("[providers.openrouter]\nbase_url='x'\n")
        ctx = _ctx(tmp_path, api_key="")
        results = check_config(ctx)
        key = next(r for r in results if r.name == "openrouter_api_key")
        assert key.status == "fail"
        assert "OPENROUTER_API_KEY" in key.suggestion


class TestLLMCheck:
    def test_reachable_probe_green(self, tmp_path):
        ctx = _ctx(tmp_path, api_key="tok")
        with patch("cli.doctor._probe_openrouter", return_value=(True, "200 OK")):
            results = check_llm(ctx)
        probe = next(r for r in results if r.name == "openrouter_reachable")
        assert probe.status == "ok"

    def test_unreachable_probe_fail(self, tmp_path):
        ctx = _ctx(tmp_path, api_key="tok")
        with patch("cli.doctor._probe_openrouter", return_value=(False, "timeout")):
            results = check_llm(ctx)
        probe = next(r for r in results if r.name == "openrouter_reachable")
        assert probe.status == "fail"

    def test_skipped_when_no_api_key(self, tmp_path):
        ctx = _ctx(tmp_path, api_key="")
        results = check_llm(ctx)
        # When no API key, the LLM check reports warn (or fail) — not ok.
        assert all(r.status != "ok" for r in results)


class TestMCPCheck:
    def test_no_servers_configured_green(self, tmp_path):
        ctx = _ctx(tmp_path)
        results = check_mcp(ctx)
        assert all(r.status != "fail" for r in results)

    def test_unhealthy_server_produces_fail(self, tmp_path):
        from kiso.mcp.config import MCPServer

        server = MCPServer(
            name="broken",
            transport="stdio",
            command="/this/does/not/exist",
            args=[],
            enabled=True,
            timeout_s=1.0,
        )
        config = Config(
            tokens={"cli": "tok"},
            providers={"openrouter": Provider(base_url="https://example.com/v1")},
            users={},
            models=full_models(),
            settings=full_settings(),
            raw={},
            mcp_servers={"broken": server},
        )
        ctx = DoctorContext(
            kiso_dir=tmp_path, config=config,
            config_path=tmp_path / "config.toml", api_key="x",
        )
        results = check_mcp(ctx)
        broken = [r for r in results if "broken" in r.name]
        assert broken
        assert any(r.status == "fail" for r in broken)


class TestSkillsCheck:
    def test_no_skills_installed_green(self, tmp_path):
        ctx = _ctx(tmp_path)
        (tmp_path / "skills").mkdir()
        results = check_skills(ctx)
        assert all(r.status != "fail" for r in results)

    def test_malformed_skill_produces_fail(self, tmp_path):
        ctx = _ctx(tmp_path)
        skill_dir = tmp_path / "skills" / "bad"
        skill_dir.mkdir(parents=True)
        # No SKILL.md → discovery skips silently; simulate a malformed one.
        (skill_dir / "SKILL.md").write_text("malformed: not yaml frontmatter")
        results = check_skills(ctx)
        # Should surface at least one warn/fail row mentioning "bad".
        bad_rows = [r for r in results if "bad" in r.name.lower()]
        assert bad_rows
        assert any(r.status in ("warn", "fail") for r in bad_rows)


class TestSandboxCheck:
    def test_non_root_is_ok(self, tmp_path):
        ctx = _ctx(tmp_path)
        with patch("cli.doctor.os.geteuid", return_value=1000):
            results = check_sandbox(ctx)
        assert all(r.status != "fail" for r in results)

    def test_root_without_useradd_fails(self, tmp_path):
        ctx = _ctx(tmp_path)

        def _which(x):
            return None if x == "useradd" else "/usr/bin/x"

        with patch("cli.doctor.os.geteuid", return_value=0), \
             patch("cli.doctor.shutil.which", side_effect=_which):
            results = check_sandbox(ctx)
        assert any(r.status == "fail" for r in results)


class TestTrustCheck:
    def test_no_trust_file_is_ok(self, tmp_path):
        ctx = _ctx(tmp_path)
        results = check_trust(ctx)
        assert all(r.status != "fail" for r in results)

    def test_malformed_trust_file_fails(self, tmp_path):
        ctx = _ctx(tmp_path)
        (tmp_path / "trust.json").write_text("not json {{{")
        results = check_trust(ctx)
        assert any(r.status == "fail" for r in results)


class TestStoreCheck:
    def test_missing_db_is_ok_on_fresh_install(self, tmp_path):
        ctx = _ctx(tmp_path)
        results = check_store(ctx)
        assert all(r.status != "fail" for r in results)
        db_file = next(r for r in results if r.name == "db_file")
        assert "store.db" in db_file.detail
        assert "kiso.db" not in db_file.detail

    def test_db_in_wal_mode_is_ok(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "store.db"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t(id integer)")
        conn.commit()
        conn.close()

        ctx = _ctx(tmp_path)
        results = check_store(ctx)
        by_name = {r.name: r for r in results}
        assert by_name["db_file"].status == "ok"
        assert str(db_path) in by_name["db_file"].detail
        assert by_name["wal_mode"].status == "ok"

    def test_db_without_wal_warns(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "store.db"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE t(id integer)")
        conn.commit()
        conn.close()

        ctx = _ctx(tmp_path)
        results = check_store(ctx)
        wal = next(r for r in results if r.name == "wal_mode")
        assert wal.status in ("warn", "fail")

    async def test_check_store_recognizes_init_db_output(self, tmp_path):
        """Drift lock: `init_db` writes the same filename `check_store` reads.

        The production callsite (`kiso/main.py`) feeds `KISO_DIR / "store.db"`
        into `init_db`. If `check_store` ever drifts back to a different
        filename, this test fails because it would report `db_file` as
        missing instead of `ok`.
        """
        from kiso.store import init_db

        db_path = tmp_path / "store.db"
        conn = await init_db(db_path)
        try:
            assert db_path.exists()
        finally:
            await conn.close()

        ctx = _ctx(tmp_path)
        results = check_store(ctx)
        by_name = {r.name: r for r in results}
        assert by_name["db_file"].status == "ok"
        assert "no " not in by_name["db_file"].detail.lower()
        assert by_name["wal_mode"].status == "ok"


class TestWorkspaceCheck:
    def test_writable_ok(self, tmp_path):
        ctx = _ctx(tmp_path)
        results = check_workspace(ctx)
        assert all(r.status != "fail" for r in results)

    def test_read_only_fs_fails(self, tmp_path):
        readonly = tmp_path / "ro"
        readonly.mkdir()
        os.chmod(readonly, stat.S_IRUSR | stat.S_IXUSR)
        ctx = DoctorContext(
            kiso_dir=readonly,
            config=None,
            config_path=readonly / "config.toml",
            api_key="x",
        )
        try:
            results = check_workspace(ctx)
            assert any(r.status == "fail" for r in results)
        finally:
            os.chmod(readonly, stat.S_IRWXU)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


class TestRenderers:
    def test_json_output_is_valid_and_stable(self):
        results = [
            CheckResult(
                category="Runtime", name="uv", status="ok",
                detail="/usr/bin/uv",
            ),
            CheckResult(
                category="Config", name="api_key", status="fail",
                detail="env var missing",
                suggestion="export OPENROUTER_API_KEY=...",
            ),
        ]
        raw = render_json(results)
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
        assert parsed[0]["category"] == "Runtime"
        assert parsed[1]["suggestion"] == "export OPENROUTER_API_KEY=..."

    def test_table_lists_every_check(self, capsys):
        results = [
            CheckResult(category="Runtime", name="uv", status="ok"),
            CheckResult(category="Config", name="api_key", status="fail",
                        suggestion="set env var"),
        ]
        text = render_table(results)
        assert "Runtime" in text
        assert "uv" in text
        assert "Config" in text
        assert "api_key" in text
        # Failed rows surface the suggestion so the operator sees what to do.
        assert "set env var" in text
