"""Runtime-focused guards for the tool -> wrapper rename (M1306).

These tests intentionally avoid source-tree string scans and doc/file-name
policing. The goal is to protect runtime invariants: the new modules import,
the legacy files are gone, and the registry uses the runtime key the
CLI/runtime expects.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "kiso"
CLI = ROOT / "cli"


class TestWrapperRenameRuntimeInvariants:
    """Verify that runtime entry points use the post-rename module layout."""

    def test_current_wrapper_modules_import(self):
        modules = {
            "kiso.wrappers": "discover_wrappers",
            "kiso.wrapper_repair": "repair_unhealthy_wrappers",
            "kiso.worker.wrapper": "_wrapper_task",
            "cli.wrapper": "run_wrapper_command",
            "kiso.recipe_loader": "discover_recipes",
            "cli.recipe": "run_recipe_command",
        }
        for module_name, attr in modules.items():
            module = importlib.import_module(module_name)
            assert hasattr(module, attr), f"{module_name} missing {attr}"

    def test_legacy_tool_files_removed(self):
        legacy_paths = [
            SRC / "tools.py",
            SRC / "tool_repair.py",
            SRC / "worker" / "tool.py",
            CLI / "tool.py",
            # Also check old skill-era files are still gone
            SRC / "skills.py",
            SRC / "skill_repair.py",
            SRC / "skill_loader.py",
            SRC / "worker" / "skill.py",
            CLI / "skill.py",
        ]
        for path in legacy_paths:
            assert not path.exists(), f"legacy file still present: {path.relative_to(ROOT)}"

    def test_registry_uses_wrappers_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "wrappers" in registry, "registry.json missing 'wrappers' key"

    def test_registry_uses_connectors_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "connectors" in registry, "registry.json missing 'connectors' key"

    def test_task_type_wrapper_value(self):
        """M1308: TASK_TYPE_WRAPPER constant must have value 'wrapper'."""
        from kiso.brain.common import TASK_TYPE_WRAPPER
        assert TASK_TYPE_WRAPPER == "wrapper", (
            f"TASK_TYPE_WRAPPER must be 'wrapper', got '{TASK_TYPE_WRAPPER}'"
        )

    def test_fact_categories_use_wrapper(self):
        """M1308: 'wrapper' in valid fact categories, 'tool' not."""
        from kiso.brain.common import _VALID_FACT_CATEGORIES
        assert "wrapper" in _VALID_FACT_CATEGORIES
        assert "tool" not in _VALID_FACT_CATEGORIES

    def test_entity_kinds_use_wrapper(self):
        """M1308: 'wrapper' in entity kinds, 'tool' not."""
        from kiso.brain.common import _ENTITY_KINDS
        assert "wrapper" in _ENTITY_KINDS
        assert "tool" not in _ENTITY_KINDS
