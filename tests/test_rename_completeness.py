"""M448: Integration test — verify skill → tool rename completeness.

Checks structural invariants: new files exist, imports point to new modules,
registry uses new keys, and no stale imports of old module names remain in
non-shim files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "kiso"
CLI = ROOT / "cli"
TESTS = ROOT / "tests"

# Backward compat shim files — these intentionally re-export old names
_SHIM_FILES = {
    SRC / "skills.py",
    SRC / "skill_repair.py",
    SRC / "worker" / "skill.py",
    CLI / "skill.py",
}


def _source_files(*, include_shims: bool = False) -> list[Path]:
    """Collect all .py files in kiso/, cli/, tests/ (excluding __pycache__)."""
    files = []
    for d in (SRC, CLI, TESTS):
        for f in d.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            if not include_shims and f.resolve() in {s.resolve() for s in _SHIM_FILES}:
                continue
            files.append(f)
    return files


class TestRenameCompleteness:
    """Verify the skill → tool rename is structurally sound."""

    def test_new_files_exist(self):
        """New tool-named files must exist."""
        assert (SRC / "tools.py").exists(), "kiso/tools.py missing"
        assert (SRC / "tool_repair.py").exists(), "kiso/tool_repair.py missing"
        assert (SRC / "worker" / "tool.py").exists(), "kiso/worker/tool.py missing"
        assert (CLI / "tool.py").exists(), "cli/tool.py missing"

    def test_shim_files_only_re_export(self):
        """Backward compat shim files must only contain re-exports."""
        for shim in _SHIM_FILES:
            if not shim.exists():
                continue  # shim already removed — fine
            content = shim.read_text()
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith('"""') or line.startswith("from "):
                    continue
                pytest.fail(
                    f"{shim.relative_to(ROOT)}: shim contains non-import line: {line}"
                )

    def test_no_direct_import_of_old_modules_in_source(self):
        """Non-shim source files must not import from old module names."""
        old_imports = [
            "from kiso.skills ",
            "import kiso.skills",
            "from kiso.skill_repair ",
            "import kiso.skill_repair",
            "from kiso.worker.skill ",
            "import kiso.worker.skill",
        ]
        violations = []
        for f in _source_files():
            # Skip this test file
            if f.name == "test_rename_completeness.py":
                continue
            content = f.read_text()
            for old in old_imports:
                if old in content:
                    violations.append(f"{f.relative_to(ROOT)}: {old}")
        assert not violations, (
            f"Found {len(violations)} stale import(s):\n" + "\n".join(violations)
        )

    def test_no_patch_of_old_module_names(self):
        """Test patches must reference new module names."""
        old_patches = [
            '"kiso.brain.discover_skills"',
            '"kiso.worker.loop.discover_skills"',
            '"kiso.worker.loop._skill_task"',
            '"kiso.worker.discover_skills"',
        ]
        violations = []
        for f in _source_files():
            if f.name == "test_rename_completeness.py":
                continue
            content = f.read_text()
            for old in old_patches:
                if old in content:
                    violations.append(f"{f.relative_to(ROOT)}: {old}")
        assert not violations, (
            f"Found {len(violations)} stale patch target(s):\n" + "\n".join(violations)
        )

    def test_registry_has_tools_key(self):
        """registry.json must have 'tools' key."""
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "tools" in registry, "registry.json missing 'tools' key"

    def test_new_test_files_exist(self):
        """Renamed test files must exist."""
        expected = [
            TESTS / "test_tools.py",
            TESTS / "test_tool_lifecycle.py",
            TESTS / "test_tool_replan.py",
            TESTS / "test_tool_install_health.py",
            TESTS / "test_tool_repair.py",
            TESTS / "docker" / "test_tool_venv.py",
        ]
        for f in expected:
            assert f.exists(), f"{f.relative_to(ROOT)} missing"

    def test_new_doc_files_exist(self):
        """Renamed doc files must exist."""
        docs = ROOT / "docs"
        assert (docs / "tools.md").exists(), "docs/tools.md missing"
        assert (docs / "tool-development.md").exists(), "docs/tool-development.md missing"
