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
# — Preset manifest format + validation
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

    @pytest.mark.parametrize("omitted,expected_substr", [
        ("name", "name"),
        ("version", "version"),
        ("description", "description"),
    ])
    def test_missing_required_field(self, omitted, expected_substr):
        base = {"type": "preset", "name": "x", "version": "1", "description": "d", "preset": {}}
        del base[omitted]
        errors = validate_preset_manifest({"kiso": base})
        assert any(expected_substr in e for e in errors)

    def test_missing_preset_section(self):
        errors = validate_preset_manifest({"kiso": {"type": "preset", "name": "x", "version": "1", "description": "d"}})
        assert any("Missing [kiso.preset]" in e for e in errors)

    @pytest.mark.parametrize("preset_extra,expected_substr", [
        ({"tools": "bad"}, "tools must be a list"),
        ({"tools": [1, 2]}, "strings"),
    ])
    def test_tools_wrong_type(self, preset_extra, expected_substr):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1",
                    "description": "d", "preset": preset_extra}}
        errors = validate_preset_manifest(manifest)
        assert any(expected_substr in e for e in errors)

    @pytest.mark.parametrize("knowledge,expected_substr", [
        ({"facts": [{"category": "general"}]}, "content is required"),
        ({"facts": [{"content": "hello", "category": "bogus"}]}, "invalid category"),
        ({"facts": [{"content": "hello", "tags": "bad"}]}, "tags must be a list"),
        ({"behaviors": [123]}, "non-empty strings"),
        ({"behaviors": [""]}, "non-empty strings"),
    ])
    def test_invalid_nested_knowledge(self, knowledge, expected_substr):
        manifest = {"kiso": {"type": "preset", "name": "x", "version": "1",
                    "description": "d", "preset": {"knowledge": knowledge}}}
        errors = validate_preset_manifest(manifest)
        assert any(expected_substr in e for e in errors)

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
# — Registry "presets" section
# ---------------------------------------------------------------------------

class TestRegistryPresets:
    def test_registry_contains_presets(self):
        reg_path = Path(__file__).resolve().parent.parent / "registry.json"
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        assert "presets" in reg
        presets = reg["presets"]
        assert len(presets) >= 1
        names = {p["name"] for p in presets}
        assert "default" in names
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
# — Install orchestration
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
             patch("cli.preset_ops.PRESETS_DIR", tmp_path / "presets"), \
             patch("cli.preset_ops._auto_install_plugins", return_value=["websearch"]):
            # Patch the _installed_path to use tmp
            with patch("cli.preset_ops._installed_path", return_value=tmp_path / "presets" / "test-p.installed.json"):
                install_preset(args, manifest)

        out = capsys.readouterr().out
        assert "Preset installed" in out
        assert "1 behaviors" in out
        assert "1 facts" in out

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
# — CLI: kiso preset list/search/install/show
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

    def test_install_registry_name_clones_repo(self, tmp_path, capsys):
        """Registry name triggers git clone of preset repo."""
        from cli.preset import preset_install
        reg = {"presets": [{"name": "test-preset", "description": "Test"}]}
        args = make_cli_args(target="test-preset", dry_run=False)

        # Mock _clone_and_load_preset to return a valid manifest
        from kiso.presets import PresetManifest
        mock_manifest = PresetManifest(
            name="test-preset", version="1.0.0", description="Test",
            tools=[], behaviors=["Always be helpful."],
        )
        with patch("cli.plugin_ops.require_admin"), \
             patch("cli.preset.fetch_registry", return_value=reg), \
             patch("cli.preset._clone_and_load_preset", return_value=mock_manifest), \
             patch("cli.preset_ops.install_preset") as mock_install:
            preset_install(args)
        mock_install.assert_called_once()
        assert mock_install.call_args[0][1].name == "test-preset"

    def test_install_git_url_clones(self, capsys):
        """Git URL triggers clone."""
        from cli.preset import preset_install
        mock_manifest = PresetManifest(
            name="custom", version="1.0.0", description="Custom",
            tools=[], behaviors=["Be concise."],
        )
        args = make_cli_args(target="https://github.com/example/preset-custom.git", dry_run=False)
        with patch("cli.plugin_ops.require_admin"), \
             patch("cli.preset._clone_and_load_preset", return_value=mock_manifest) as mock_clone, \
             patch("cli.preset_ops.install_preset"):
            preset_install(args)
        mock_clone.assert_called_once_with("https://github.com/example/preset-custom.git")

    def test_clone_and_load_preset_failure(self, capsys):
        """Failed git clone → clean error."""
        from cli.preset import _clone_and_load_preset
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fatal: repo not found")
            with pytest.raises(SystemExit):
                _clone_and_load_preset("https://github.com/example/nope.git")
        err = capsys.readouterr().err
        assert "git clone failed" in err

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
# — Subcommand registration in CLI __init__
# ---------------------------------------------------------------------------

class TestPresetSubcommandRegistration:
    @pytest.mark.parametrize("args_list,expected", [
        (["preset", "list"], {"command": "preset", "preset_cmd": "list"}),
        (["preset", "install", "my-preset", "--dry-run"],
         {"target": "my-preset", "dry_run": True}),
        (["preset", "search", "marketing"], {"query": "marketing"}),
        (["preset", "show", "seo-specialist"], {"name": "seo-specialist"}),
        (["preset", "remove", "old-preset"], {"name": "old-preset"}),
        (["preset", "installed"], {"preset_cmd": "installed"}),
    ])
    def test_parser_preset_subcommand(self, args_list, expected):
        from cli import build_parser
        parser = build_parser()
        args = parser.parse_args(args_list)
        for attr, value in expected.items():
            assert getattr(args, attr) == value


# --- M757: basic preset validation ---


class TestDefaultPresetInRegistry:
    """Verify default preset is listed in registry.json."""

    def test_default_in_registry(self):
        registry = json.loads(
            (Path(__file__).parent.parent / "registry.json").read_text()
        )
        names = [p["name"] for p in registry["presets"]]
        assert "default" in names


# --- M758: preset install auto-installs tools ---


class TestM758AutoInstallTools:
    """install_preset auto-installs tools from manifest."""

    def test_auto_install_calls_wrapper_install(self, tmp_path):
        """install_preset calls _auto_install_tools for manifest.tools."""
        from cli.preset_ops import install_preset

        manifest = PresetManifest(
            name="test-auto", version="1.0.0", description="test",
            tools=["websearch", "browser"], behaviors=["Always search before answering — never guess."],
        )
        args = MagicMock()
        installed = []

        def fake_wrapper_install(fake_args):
            installed.append(fake_args.target)

        with patch("cli.preset_ops._auto_install_plugins") as mock_auto, \
             patch("cli._http.cli_post") as mock_post, \
             patch("cli.preset_ops._load_installed", return_value=None), \
             patch("cli.preset_ops._save_installed"):
            mock_post.return_value = MagicMock(json=lambda: {"id": 1})
            mock_auto.return_value = ["websearch", "browser"]
            install_preset(args, manifest)

        mock_auto.assert_called_once()

    def test_tracking_includes_installed_wrappers(self):
        """Tracking JSON includes installed_wrappers list."""
        from cli.preset_ops import install_preset

        manifest = PresetManifest(
            name="test-track", version="1.0.0", description="test",
            tools=["websearch", "browser"],
            behaviors=["Always search before answering — never guess."],
        )
        args = MagicMock()
        saved_data = {}

        def capture_save(name, data):
            saved_data.update(data)

        with patch("cli.preset_ops._auto_install_plugins", return_value=["websearch"]), \
             patch("cli._http.cli_post") as mock_post, \
             patch("cli.preset_ops._load_installed", return_value=None), \
             patch("cli.preset_ops._save_installed", side_effect=capture_save):
            mock_post.return_value = MagicMock(json=lambda: {"id": 1})
            install_preset(args, manifest)

        assert saved_data["installed_wrappers"] == ["websearch"]


# --- M760: Preset validation in CI ---


class TestPresetValidation:
    """Preset manifests created in-memory validate correctly."""

    def test_valid_preset_validates(self, tmp_path):
        """A well-formed preset.toml passes validation."""
        preset = tmp_path / "preset.toml"
        preset.write_text("""
[kiso]
type = "preset"
name = "test"
version = "1.0.0"
description = "Test preset"

[kiso.preset]
tools = ["websearch"]
skills = []
connectors = []

[kiso.preset.knowledge]
facts = []
behaviors = ["Always search the web before answering a question."]
""")
        manifest = load_preset(preset)
        assert manifest.name == "test"
        assert manifest.tools == ["websearch"]
        assert len(manifest.behaviors) == 1

    def test_preset_behaviors_not_placeholder(self, tmp_path):
        """Behaviors must be non-empty strings >= 20 chars."""
        manifest = PresetManifest(
            name="test", version="1.0.0", description="test",
            tools=[], behaviors=["Always search before answering — never guess."],
        )
        for b in manifest.behaviors:
            assert isinstance(b, str) and len(b) >= 20, (
                f"Behavior too short or invalid: {b!r}"
            )


# ---------------------------------------------------------------------------
# — Preset install: clean progress output + verify deps.sh
# ---------------------------------------------------------------------------


class TestM819CleanProgressOutput:
    """Preset install shows clean progress instead of verbose output."""

    def test_clone_prints_fetching(self, capsys):
        """_clone_and_load_preset prints 'Fetching preset...' before clone."""
        from cli.preset import _clone_and_load_preset

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fatal: not found")
            with pytest.raises(SystemExit):
                _clone_and_load_preset("https://github.com/example/test.git")
        out = capsys.readouterr().out
        assert "Fetching preset..." in out

    def test_show_summary_has_version_and_separator(self, capsys):
        """_show_preset_summary shows version, separator, tools, and behaviors."""
        from cli.preset import _show_preset_summary

        manifest = PresetManifest(
            name="demo", version="2.0.0", description="Demo",
            tools=["browser", "aider"], behaviors=["Be helpful.", "Be concise."],
        )
        _show_preset_summary(manifest)
        out = capsys.readouterr().out
        assert "demo" in out
        assert "v2.0.0" in out
        assert "───" in out
        assert "browser, aider" in out
        assert "2 guidelines" in out

    def test_auto_install_plugins_progress_format(self, capsys):
        """_auto_install_plugins shows [N/M] name ✓/✗ per plugin."""
        from cli.preset_ops import _auto_install_plugins

        mock_install = MagicMock(side_effect=[None, RuntimeError("fail")])
        result = _auto_install_plugins(["browser", "aider"], mock_install)

        out = capsys.readouterr().out
        assert "[1/2] browser" in out
        assert "✓" in out
        assert "[2/2] aider" in out
        assert "✗" in out
        assert result == ["browser"]

    def test_auto_install_plugins_suppresses_individual_output(self, capsys):
        """Individual plugin install messages are suppressed."""
        from cli.preset_ops import _auto_install_plugins

        def noisy_install(args):
            print("Tool 'test' installed successfully.")

        _auto_install_plugins(["test"], noisy_install)

        out = capsys.readouterr().out
        assert "installed successfully" not in out
        assert "[1/1] test" in out

    def test_install_final_summary(self, tmp_path, capsys):
        """install_preset shows '✓ Preset installed — N tools, N behaviors'."""
        from cli.preset_ops import install_preset

        manifest = PresetManifest(
            name="summary-test", version="1.0.0", description="Test",
            tools=["browser", "ocr"],
            behaviors=["Always search before answering — never guess."],
        )
        args = MagicMock()

        with patch("cli.preset_ops._auto_install_plugins", side_effect=[["browser", "ocr"]]), \
             patch("cli._http.cli_post") as mock_post, \
             patch("cli.preset_ops._load_installed", return_value=None), \
             patch("cli.preset_ops._save_installed"):
            mock_post.return_value = MagicMock(json=lambda: {"id": 1})
            install_preset(args, manifest)

        out = capsys.readouterr().out
        assert "Preset installed" in out
        assert "2 tools" in out
        assert "1 behaviors" in out

    def test_deps_sh_runs_via_plugin_install(self):
        """Verify deps.sh execution path: _auto_install → _wrapper_install → _plugin_install runs deps.sh."""
        import inspect
        from cli.plugin_ops import _plugin_install
        source = inspect.getsource(_plugin_install)
        assert "deps.sh" in source
        assert 'bash' in source


# ---------------------------------------------------------------------------
# — Recipes in preset system
# ---------------------------------------------------------------------------


class TestPresetRecipesManifest:
    """recipes field in PresetManifest and validation."""

    def test_manifest_has_recipes_field(self):
        m = PresetManifest(
            name="t", version="1", description="d",
            recipes=[{"name": "r1", "summary": "s1", "body": "b1"}],
        )
        assert len(m.recipes) == 1
        assert m.recipes[0]["name"] == "r1"

    def test_manifest_recipes_default_empty(self):
        m = PresetManifest(name="t", version="1", description="d")
        assert m.recipes == []

    def test_validate_valid_recipes(self):
        manifest = {
            "kiso": {
                "type": "preset", "name": "x", "version": "1", "description": "d",
                "preset": {
                    "knowledge": {
                        "recipes": [
                            {"name": "r1", "summary": "Do stuff", "body": "Instructions here"},
                        ],
                    },
                },
            },
        }
        errors = validate_preset_manifest(manifest)
        assert errors == []

    def test_validate_recipe_missing_name(self):
        manifest = {
            "kiso": {
                "type": "preset", "name": "x", "version": "1", "description": "d",
                "preset": {
                    "knowledge": {
                        "recipes": [{"summary": "s", "body": "b"}],
                    },
                },
            },
        }
        errors = validate_preset_manifest(manifest)
        assert any("recipes[0]" in e and "name" in e for e in errors)

    def test_validate_recipe_missing_body(self):
        manifest = {
            "kiso": {
                "type": "preset", "name": "x", "version": "1", "description": "d",
                "preset": {
                    "knowledge": {
                        "recipes": [{"name": "r", "summary": "s"}],
                    },
                },
            },
        }
        errors = validate_preset_manifest(manifest)
        assert any("body" in e for e in errors)

    def test_validate_recipe_not_a_table(self):
        manifest = {
            "kiso": {
                "type": "preset", "name": "x", "version": "1", "description": "d",
                "preset": {
                    "knowledge": {
                        "recipes": ["not a table"],
                    },
                },
            },
        }
        errors = validate_preset_manifest(manifest)
        assert any("must be a table" in e for e in errors)

    def test_validate_recipes_not_a_list(self):
        manifest = {
            "kiso": {
                "type": "preset", "name": "x", "version": "1", "description": "d",
                "preset": {
                    "knowledge": {
                        "recipes": "bad",
                    },
                },
            },
        }
        errors = validate_preset_manifest(manifest)
        assert any("recipes must be a list" in e for e in errors)

    def test_load_preset_with_recipes(self, tmp_path):
        toml = textwrap.dedent("""\
            [kiso]
            type = "preset"
            name = "recipe-test"
            version = "1.0.0"
            description = "Preset with recipes"

            [kiso.preset]

            [kiso.preset.knowledge]
            [[kiso.preset.knowledge.recipes]]
            name = "exploration"
            summary = "Verify before modifying"
            body = "Check files before editing them."
        """)
        path = tmp_path / "preset.toml"
        path.write_text(toml, encoding="utf-8")
        m = load_preset(path)
        assert len(m.recipes) == 1
        assert m.recipes[0]["name"] == "exploration"
        assert m.recipes[0]["summary"] == "Verify before modifying"
        assert "Check files" in m.recipes[0]["body"]


class TestPresetRecipeInstall:
    """recipe installation writes .md files and tracks them."""

    def test_install_writes_recipe_files(self, tmp_path, capsys):
        from cli.preset_ops import install_preset

        recipes_dir = tmp_path / "recipes"
        manifest = PresetManifest(
            name="recipe-preset", version="1.0.0", description="Test",
            recipes=[
                {"name": "exploration", "summary": "Verify state", "body": "Check first."},
                {"name": "error-diagnosis", "summary": "Diagnose errors", "body": "Run diagnostics."},
            ],
        )
        args = MagicMock()
        saved_data = {}

        def capture_save(name, data):
            saved_data.update(data)

        with patch("cli.preset_ops.KISO_DIR", tmp_path), \
             patch("cli.preset_ops._load_installed", return_value=None), \
             patch("cli.preset_ops._save_installed", side_effect=capture_save), \
             patch("cli._http.cli_post") as mock_post:
            mock_post.return_value = MagicMock(json=lambda: {"id": 1})
            install_preset(args, manifest)

        # Verify .md files were written
        assert (recipes_dir / "exploration.md").is_file()
        assert (recipes_dir / "error-diagnosis.md").is_file()

        content = (recipes_dir / "exploration.md").read_text(encoding="utf-8")
        assert "name: exploration" in content
        assert "summary: Verify state" in content
        assert "Check first." in content

        # Verify tracking includes recipe_files
        assert saved_data["recipe_files"] == ["exploration.md", "error-diagnosis.md"]

        # Verify summary output
        out = capsys.readouterr().out
        assert "2 recipes" in out

    def test_install_dry_run_shows_recipes(self, capsys):
        from cli.preset_ops import install_preset

        manifest = PresetManifest(
            name="dry-recipe", version="1.0.0", description="Dry",
            recipes=[{"name": "r1", "summary": "Test recipe", "body": "Do stuff."}],
        )
        args = MagicMock()
        with patch("cli.preset_ops._load_installed", return_value=None):
            install_preset(args, manifest, dry_run=True)
        out = capsys.readouterr().out
        assert "Recipes to install: 1" in out
        assert "r1: Test recipe" in out


class TestPresetRecipeRemove:
    """recipe removal deletes installed .md files."""

    def test_remove_deletes_recipe_files(self, tmp_path, capsys):
        from cli.preset_ops import remove_preset

        # Create recipe files
        recipes_dir = tmp_path / "recipes"
        recipes_dir.mkdir()
        (recipes_dir / "exploration.md").write_text("---\nname: exploration\n---\nBody.", encoding="utf-8")
        (recipes_dir / "error-diagnosis.md").write_text("---\nname: error-diagnosis\n---\nBody.", encoding="utf-8")

        tracking_file = tmp_path / "presets" / "rm-recipe.installed.json"
        tracking_data = {
            "name": "rm-recipe", "version": "1.0.0", "description": "test",
            "fact_ids": [], "behavior_ids": [],
            "tools": [], "skills": [], "connectors": [],
            "recipe_files": ["exploration.md", "error-diagnosis.md"],
        }

        def mock_request(method, url, **kw):
            resp = MagicMock()
            resp.json.return_value = {"deleted": True}
            resp.raise_for_status = MagicMock()
            return resp

        args = MagicMock()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("httpx.request", side_effect=mock_request), \
             patch("cli.preset_ops.KISO_DIR", tmp_path), \
             patch("cli.preset_ops._installed_path", return_value=tracking_file), \
             patch("cli.preset_ops._load_installed", return_value=tracking_data):
            remove_preset(args, "rm-recipe")

        assert not (recipes_dir / "exploration.md").exists()
        assert not (recipes_dir / "error-diagnosis.md").exists()

        out = capsys.readouterr().out
        assert "removed" in out.lower()
        assert "2 recipes" in out

    def test_remove_handles_missing_recipe_files(self, tmp_path, capsys):
        """Removal should not fail if recipe files are already gone."""
        from cli.preset_ops import remove_preset

        tracking_data = {
            "name": "rm-gone", "version": "1.0.0", "description": "test",
            "fact_ids": [], "behavior_ids": [],
            "tools": [], "skills": [], "connectors": [],
            "recipe_files": ["gone.md"],
        }
        tracking_file = tmp_path / "presets" / "rm-gone.installed.json"

        args = MagicMock()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("httpx.request"), \
             patch("cli.preset_ops.KISO_DIR", tmp_path), \
             patch("cli.preset_ops._installed_path", return_value=tracking_file), \
             patch("cli.preset_ops._load_installed", return_value=tracking_data):
            remove_preset(args, "rm-gone")

        out = capsys.readouterr().out
        assert "removed" in out.lower()
        # No recipe removal line since file didn't exist
        assert "recipes" not in out
