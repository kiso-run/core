"""Runtime-focused guards for the historical skill -> tool rename.

These tests intentionally avoid source-tree string scans and doc/file-name
policing. The goal is to protect runtime invariants that still matter:
the new modules import, the legacy shim files are gone, and the registry uses
the runtime key the CLI/runtime expects.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "kiso"
CLI = ROOT / "cli"


class TestRenameRuntimeInvariants:
    """Verify that runtime entry points use the post-rename module layout."""

    def test_current_tool_modules_import(self):
        modules = {
            "kiso.tools": "discover_tools",
            "kiso.tool_repair": "repair_unhealthy_tools",
            "kiso.worker.tool": "_tool_task",
            "cli.tool": "run_tool_command",
            "kiso.recipe_loader": "discover_recipes",
            "cli.recipe": "run_recipe_command",
        }
        for module_name, attr in modules.items():
            module = importlib.import_module(module_name)
            assert hasattr(module, attr), f"{module_name} missing {attr}"

    def test_legacy_shim_files_removed(self):
        legacy_paths = [
            SRC / "skills.py",
            SRC / "skill_repair.py",
            SRC / "skill_loader.py",
            SRC / "worker" / "skill.py",
            CLI / "skill.py",
        ]
        for path in legacy_paths:
            assert not path.exists(), f"legacy shim still present: {path.relative_to(ROOT)}"

    def test_registry_uses_tools_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "tools" in registry, "registry.json missing 'tools' key"

    def test_registry_uses_connectors_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "connectors" in registry, "registry.json missing 'connectors' key"
