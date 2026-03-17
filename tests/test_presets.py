"""M690-M694: Tests for preset manifest, registry, CLI, and orchestration."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiso.presets import PresetManifest, load_preset, validate_preset_manifest
from tests._cli_test_helpers import mock_cli_config, make_cli_args, mock_http_response


_VALID_TOML = """\
[kiso]
type = "preset"
name = "test-preset"
version = "0.1.0"
description = "A test persona preset"

[kiso.preset]
tools = ["websearch", "browser"]
skills = ["ad-copy"]
connectors = []

[kiso.preset.knowledge]
facts = [
    { content = "Optimize for ROI and conversion rate", category = "project", tags = ["marketing"] },
]
behaviors = [
    "Always cite data sources when making claims",
]

[kiso.preset.env]
SEMRUSH_API_KEY = { required = false, description = "SEMrush API key" }
"""


def _write_preset_toml(tmp_path: Path, content: str = _VALID_TOML) -> Path:
    p = tmp_path / "preset.toml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# M690 — Preset manifest format + validation
# ---------------------------------------------------------------------------

class TestValidatePresetManifest:
    def test_valid_manifest(self):
        import tomllib
        manifest = tomllib.loads(_VALID_TOML)
        errors = validate_preset_manifest(manifest)
        assert errors == []

    def test_missing_kiso_section(self):
        errors = validate_preset_manifest({})
        assert any("Missing [kiso] section" in e for e in errors)

    def test_wrong_type(self):
        errors = validate_preset_manifest({"kiso": {"type": "tool", "name": "x", "version": "1", "description": "d", "preset": {}}})
        assert any("type must be 'preset'" in e for e in errors)

    def test_missing_name(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "version": "1", "description": "d", "preset": {}}})
        assert any("name" in e for e in errors)

    def test_missing_version(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "name": "x", "description": "d", "preset": {}}})
        assert any("version" in e for e in errors)

    def test_missing_description(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "name": "x", "version": "1", "preset": {}}})
        assert any("description" in e for e in errors)

    def test_missing_preset_section(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d"}})
        assert any("Missing [kiso.preset]" in e for e in errors)

    def test_tools_must_be_list(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d", "preset": {"tools": "bad"}}})
        assert any("tools must be a list" in e for e in errors)

    def test_tools_must_contain_strings(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d", "preset": {"tools": [1, 2]}}})
        assert any("strings" in e for e in errors)

    def test_fact_missing_content(self):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d",
                    "preset": {"knowledge": {"facts": [{"category": "general"}]}}}}
        errors = validate_preset_manifest(manifest)
        assert any("content is required" in e for e in errors)

    def test_fact_invalid_category(self):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d",
                    "preset": {"knowledge": {"facts": [{"content": "hello", "category": "bogus"}]}}}}
        errors = validate_preset_manifest(manifest)
        assert any("invalid category" in e for e in errors)

    def test_fact_tags_must_be_list(self):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d",
                    "preset": {"knowledge": {"facts": [{"content": "hello", "tags": "bad"}]}}}}
        errors = validate_preset_manifest(manifest)
        assert any("tags must be a list" in e for e in errors)

    def test_behaviors_must_be_strings(self):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d",
                    "preset": {"knowledge": {"behaviors": [123]}}}}
        errors = validate_preset_manifest(manifest)
        assert any("non-empty strings" in e for e in errors)

    def test_empty_behavior_rejected(self):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d",
                    "preset": {"knowledge": {"behaviors": [""]}}}}
        errors = validate_preset_manifest(manifest)
        assert any("non-empty strings" in e for e in errors)

    def test_env_value_must_be_table(self):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d",
                    "preset": {"env": {"KEY": "bad"}}}}
        errors = validate_preset_manifest(manifest)
        assert any("must be a table" in e for e in errors)


class TestLoadPreset:
    def test_load_valid(self, tmp_path):
        path = _write_preset_toml(tmp_path)
        m = load_preset(path)
        assert isinstance(m, PresetManifest)
        assert m.name == "test-preset"
        assert m.version == "0.1.0"
        assert m.tools == ["websearch", "browser"]
        assert m.skills == ["ad-copy"]
        assert len(m.knowledge_facts) == 1
        assert len(m.behaviors) == 1
        assert "SEMRUSH_API_KEY" in m.env_vars

    def test_load_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_preset(tmp_path / "nope.toml")

    def test_load_invalid_manifest(self, tmp_path):
        path = tmp_path / "bad.toml"
        path.write_text("[kiso]\ntype = 'tool'\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid preset manifest"):
            load_preset(path)

    def test_load_empty_tools(self, tmp_path):
        toml = textwrap.dedent("""\
            [kiso]
            type = "preset"
            name = "minimal"
            version = "1.0.0"
            description = "Minimal preset"

            [kiso.preset]
        """)
        path = tmp_path / "preset.toml"
        path.write_text(toml, encoding="utf-8")
        m = load_preset(path)
        assert m.tools == []
        assert m.skills == []
        assert m.knowledge_facts == []
        assert m.behaviors == []


# ---------------------------------------------------------------------------
# M691 — Registry "presets" section
# ---------------------------------------------------------------------------

class TestRegistryPresets:
    def test_registry_contains_presets(self):
        reg_path = Path(__file__).resolve().parent.parent / "registry.json"
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        assert "presets" in reg
        presets = reg["presets"]
        assert len(presets) >= 3
        names = {p["name"] for p in presets}
        assert "performance-marketer" in names
        assert "seo-specialist" in names
        assert "backend-developer" in names
        for p in presets:
            assert "description" in p and p["description"]

    def test_search_entries_on_presets(self):
        from kiso.registry import search_entries
        presets = [
            {"name": "performance-marketer", "description": "marketing"},
            {"name": "seo-specialist", "description": "SEO optimization"},
        ]
        assert len(search_entries(presets, "seo")) == 1
        assert search_entries(presets, "seo")[0]["name"] == "seo-specialist"
        assert len(search_entries(presets, None)) == 2
        assert len(search_entries(presets, "marketing")) == 1


# ---------------------------------------------------------------------------
# M692 — Install orchestration
# ---------------------------------------------------------------------------

class TestInstallPreset:
    def test_install_seeds_facts_and_behaviors(self, tmp_path, capsys):
        from cli.preset_ops import install_preset, PRESETS_DIR

        manifest = PresetManifest(
            name="test-p", version="0.1.0", description="Test",
            tools=["websearch"],
            knowledge_facts=[{"content": "ROI matters most", "category": "project", "tags": ["kpi"]}],
            behaviors=["Always use data"],
        )

        call_count = {"n": 0}
        def mock_request(method, url, **kw):
            call_count["n"] += 1
            resp = MagicMock()
            resp.json.return_value = {"id": call_count["n"], "content": "x", "category": "general"}
            resp.raise_for_status = MagicMock()
            return resp

        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("httpx.request", side_effect=mock_request), \
             patch("cli.preset_ops.PRESETS_DIR", tmp_path / "presets"):
            # Patch the _installed_path to use tmp
            with patch("cli.preset_ops._installed_path", return_value=tmp_path / "presets" / "test-p.installed.json"):
                install_preset(args, manifest)

        out = capsys.readouterr().out
        assert "installed" in out.lower()
        assert "1 knowledge facts" in out
        assert "1 behaviors" in out
        assert "kiso tool install websearch" in out

    def test_install_dry_run(self, capsys):
        manifest = PresetManifest(
            name="dry-test", version="1.0.0", description="Dry",
            tools=["browser"], behaviors=["Be concise always"],
        )
        args = make_cli_args()
        from cli.preset_ops import install_preset
        install_preset(args, manifest, dry_run=True)
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "browser" in out
        assert "Be concise" in out

    def test_install_already_installed(self, tmp_path, capsys):
        from cli.preset_ops import install_preset

        manifest = PresetManifest(name="dup", version="1.0.0", description="Dup")

        # Create tracking file
        tracking_dir = tmp_path / "presets"
        tracking_dir.mkdir()
        tracking_file = tracking_dir / "dup.installed.json"
        tracking_file.write_text('{"name": "dup"}', encoding="utf-8")

        args = make_cli_args()
        with patch("cli.preset_ops._installed_path", return_value=tracking_file), \
             patch("cli.preset_ops._load_installed", return_value={"name": "dup"}):
            install_preset(args, manifest)
        out = capsys.readouterr().out
        assert "already installed" in out.lower()


class TestRemovePreset:
    def test_remove_deletes_facts(self, tmp_path, capsys):
        from cli.preset_ops import remove_preset

        tracking_dir = tmp_path / "presets"
        tracking_dir.mkdir()
        tracking_file = tracking_dir / "rm-test.installed.json"
        tracking_data = {
            "name": "rm-test", "version": "1.0.0", "description": "test",
            "fact_ids": [10, 11], "behavior_ids": [20],
            "tools": [], "skills": [], "connectors": [],
        }
        tracking_file.write_text(json.dumps(tracking_data), encoding="utf-8")

        def mock_request(method, url, **kw):
            resp = MagicMock()
            resp.json.return_value = {"deleted": True}
            resp.raise_for_status = MagicMock()
            return resp

        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("httpx.request", side_effect=mock_request), \
             patch("cli.preset_ops._installed_path", return_value=tracking_file), \
             patch("cli.preset_ops._load_installed", return_value=tracking_data):
            remove_preset(args, "rm-test")

        out = capsys.readouterr().out
        assert "removed" in out.lower()
        assert "2 knowledge facts" in out
        assert "1 behaviors" in out
        assert not tracking_file.exists()

    def test_remove_not_installed(self, capsys):
        from cli.preset_ops import remove_preset
        args = make_cli_args()
        with patch("cli.preset_ops._load_installed", return_value=None), \
             pytest.raises(SystemExit):
            remove_preset(args, "nope")
        assert "not installed" in capsys.readouterr().err


class TestListInstalledPresets:
    def test_empty(self, tmp_path):
        from cli.preset_ops import list_installed_presets
        with patch("cli.preset_ops.PRESETS_DIR", tmp_path / "empty"):
            result = list_installed_presets()
        assert result == []

    def test_with_presets(self, tmp_path):
        from cli.preset_ops import list_installed_presets
        presets_dir = tmp_path / "presets"
        presets_dir.mkdir()
        (presets_dir / "a.installed.json").write_text(
            json.dumps({"name": "a", "version": "1"}), encoding="utf-8")
        (presets_dir / "b.installed.json").write_text(
            json.dumps({"name": "b", "version": "2"}), encoding="utf-8")
        with patch("cli.preset_ops.PRESETS_DIR", presets_dir):
            result = list_installed_presets()
        assert len(result) == 2
        assert result[0]["name"] == "a"


# ---------------------------------------------------------------------------
# M693 — CLI: kiso preset list/search/install/show
# ---------------------------------------------------------------------------

class TestPresetListCLI:
    def test_list_shows_presets(self, capsys):
        from cli.preset import preset_list
        reg = {"presets": [
            {"name": "performance-marketer", "description": "Marketing"},
            {"name": "seo-specialist", "description": "SEO"},
        ]}
        args = make_cli_args()
        with patch("cli.preset.fetch_registry", return_value=reg):
            preset_list(args)
        out = capsys.readouterr().out
        assert "performance-marketer" in out
        assert "seo-specialist" in out

    def test_list_empty_registry(self, capsys):
        from cli.preset import preset_list
        args = make_cli_args()
        with patch("cli.preset.fetch_registry", return_value={"presets": []}):
            preset_list(args)
        assert "No presets" in capsys.readouterr().out


class TestPresetSearchCLI:
    def test_search_by_name(self, capsys):
        from cli.preset import preset_search
        reg = {"presets": [
            {"name": "performance-marketer", "description": "Marketing"},
            {"name": "seo-specialist", "description": "SEO"},
        ]}
        args = make_cli_args(query="seo")
        with patch("cli.preset.fetch_registry", return_value=reg):
            preset_search(args)
        out = capsys.readouterr().out
        assert "seo-specialist" in out
        assert "performance-marketer" not in out

    def test_search_no_results(self, capsys):
        from cli.preset import preset_search
        reg = {"presets": [{"name": "a", "description": "b"}]}
        args = make_cli_args(query="nonexistent")
        with patch("cli.preset.fetch_registry", return_value=reg):
            preset_search(args)
        assert "No presets matching" in capsys.readouterr().out


class TestPresetInstallCLI:
    def test_install_from_local_path(self, tmp_path, capsys):
        from cli.preset import preset_install

        path = _write_preset_toml(tmp_path)
        args = make_cli_args(target=str(path), dry_run=True)

        with patch("cli.plugin_ops.require_admin"):
            preset_install(args)
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "test-preset" in out

    def test_install_from_directory(self, tmp_path, capsys):
        from cli.preset import preset_install

        _write_preset_toml(tmp_path)
        args = make_cli_args(target=str(tmp_path), dry_run=True)

        with patch("cli.plugin_ops.require_admin"):
            preset_install(args)
        out = capsys.readouterr().out
        assert "Dry run" in out

    def test_install_registry_name_shows_instructions(self, capsys):
        from cli.preset import preset_install
        reg = {"presets": [{"name": "seo-specialist", "description": "SEO"}]}
        args = make_cli_args(target="seo-specialist", dry_run=False)
        with patch("cli.plugin_ops.require_admin"), \
             patch("cli.preset.fetch_registry", return_value=reg), \
             pytest.raises(SystemExit):
            preset_install(args)
        err = capsys.readouterr().err
        assert "requires a local preset.toml" in err

    def test_install_unknown_name_errors(self, capsys):
        from cli.preset import preset_install
        reg = {"presets": []}
        args = make_cli_args(target="nonexistent", dry_run=False)
        with patch("cli.plugin_ops.require_admin"), \
             patch("cli.preset.fetch_registry", return_value=reg), \
             pytest.raises(SystemExit):
            preset_install(args)
        assert "not found" in capsys.readouterr().err


class TestPresetShowCLI:
    def test_show_local_file(self, tmp_path, capsys):
        from cli.preset import preset_show
        path = _write_preset_toml(tmp_path)
        args = make_cli_args(name=str(path))
        preset_show(args)
        out = capsys.readouterr().out
        assert "test-preset" in out
        assert "websearch" in out

    def test_show_installed(self, capsys):
        from cli.preset import preset_show
        tracking = {
            "name": "my-preset", "version": "1.0.0", "description": "My preset",
            "tools": ["browser"], "skills": [], "connectors": [],
            "fact_ids": [1, 2], "behavior_ids": [3],
        }
        args = make_cli_args(name="my-preset")
        with patch("cli.preset_ops._load_installed", return_value=tracking):
            preset_show(args)
        out = capsys.readouterr().out
        assert "my-preset" in out
        assert "browser" in out
        assert "2 seeded" in out

    def test_show_registry(self, capsys):
        from cli.preset import preset_show
        reg = {"presets": [{"name": "seo-specialist", "description": "SEO stuff"}]}
        args = make_cli_args(name="seo-specialist")
        with patch("cli.preset_ops._load_installed", return_value=None), \
             patch("cli.preset.fetch_registry", return_value=reg):
            preset_show(args)
        out = capsys.readouterr().out
        assert "seo-specialist" in out
        assert "Not installed" in out

    def test_show_not_found(self, capsys):
        from cli.preset import preset_show
        reg = {"presets": []}
        args = make_cli_args(name="nope")
        with patch("cli.preset_ops._load_installed", return_value=None), \
             patch("cli.preset.fetch_registry", return_value=reg), \
             pytest.raises(SystemExit):
            preset_show(args)
        assert "not found" in capsys.readouterr().err


class TestPresetInstalledCLI:
    def test_no_presets(self, capsys):
        from cli.preset import preset_installed
        args = make_cli_args()
        with patch("cli.preset_ops.list_installed_presets", return_value=[]):
            preset_installed(args)
        assert "No presets installed" in capsys.readouterr().out

    def test_with_presets(self, capsys):
        from cli.preset import preset_installed
        presets = [
            {"name": "a", "version": "1.0.0", "description": "Alpha",
             "fact_ids": [1, 2], "behavior_ids": [3], "tools": ["browser"], "skills": []},
        ]
        args = make_cli_args()
        with patch("cli.preset_ops.list_installed_presets", return_value=presets):
            preset_installed(args)
        out = capsys.readouterr().out
        assert "a" in out
        assert "v1.0.0" in out
        assert "2 facts" in out
        assert "browser" in out


class TestPresetRemoveCLI:
    def test_remove_calls_ops(self, capsys):
        from cli.preset import preset_remove
        args = make_cli_args(name="test-rm")
        with patch("cli.plugin_ops.require_admin"), \
             patch("cli.preset_ops.remove_preset") as mock_rm:
            preset_remove(args)
        mock_rm.assert_called_once_with(args, "test-rm")


# ---------------------------------------------------------------------------
# M694 — Subcommand registration in CLI __init__
# ---------------------------------------------------------------------------

class TestPresetSubcommandRegistration:
    def test_parser_has_preset_command(self):
        from cli import build_parser
        parser = build_parser()
        # Just verify it parses without error
        args = parser.parse_args(["preset", "list"])
        assert args.command == "preset"
        assert args.preset_cmd == "list"

    def test_parser_preset_install(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["preset", "install", "my-preset", "--dry-run"])
        assert args.target == "my-preset"
        assert args.dry_run is True

    def test_parser_preset_search(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["preset", "search", "marketing"])
        assert args.query == "marketing"

    def test_parser_preset_show(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["preset", "show", "seo-specialist"])
        assert args.name == "seo-specialist"

    def test_parser_preset_remove(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["preset", "remove", "old-preset"])
        assert args.name == "old-preset"

    def test_parser_preset_installed(self):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["preset", "installed"])
        assert args.preset_cmd == "installed"
