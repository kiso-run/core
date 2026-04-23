"""Runtime-focused guards for the wrapper rename.

These tests protect runtime invariants: the correct modules import,
legacy files are gone, and the registry/schema use the expected names.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "kiso"
CLI = ROOT / "cli"


class TestWrapperRenameRuntimeInvariants:
    """Verify that runtime entry points use the post-rename module layout."""

    def test_legacy_files_removed(self):
        legacy_paths = [
            SRC / "tools.py",
            SRC / "tool_repair.py",
            SRC / "worker" / "tool.py",
            CLI / "tool.py",
            SRC / "skills.py",
            SRC / "skill_repair.py",
            SRC / "worker" / "skill.py",
            # skill_loader.py, skill_runtime.py: v0.10 re-introduces these
            # as first-class modules (M1500/M1501). `cli/skill.py` is
            # expected to arrive with M1506.
        ]
        for path in legacy_paths:
            assert not path.exists(), f"legacy file still present: {path.relative_to(ROOT)}"

    def test_registry_uses_wrappers_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "wrappers" in registry, "registry.json missing 'wrappers' key"

    def test_registry_uses_connectors_key(self):
        registry = json.loads((ROOT / "registry.json").read_text())
        assert "connectors" in registry, "registry.json missing 'connectors' key"

    def test_fact_categories_use_wrapper(self):
        from kiso.brain.common import _VALID_FACT_CATEGORIES
        assert "wrapper" in _VALID_FACT_CATEGORIES
        assert "tool" not in _VALID_FACT_CATEGORIES

    def test_entity_kinds_use_wrapper(self):
        from kiso.brain.common import _ENTITY_KINDS
        assert "wrapper" in _ENTITY_KINDS
        assert "tool" not in _ENTITY_KINDS

    def test_db_tasks_column_is_wrapper(self):
        """: DB schema uses 'wrapper' column, not 'skill'."""
        from kiso.store.shared import SCHEMA
        assert "wrapper" in SCHEMA
        assert "skill" not in SCHEMA

    def test_create_task_uses_wrapper_kwarg(self):
        """: create_task() accepts wrapper= kwarg, not skill=."""
        from kiso.store.plans import create_task
        sig = inspect.signature(create_task)
        assert "wrapper" in sig.parameters
        assert "skill" not in sig.parameters

    def test_cli_user_skills_and_mcp_flags(self):
        """CLI user command exposes --skills and --mcp, not --wrappers."""
        import cli.user as user_mod
        src = inspect.getsource(user_mod)
        assert '"skills"' in src
        assert '"mcp"' in src
        # The retired --wrappers flag must not be referenced.
        assert "--wrappers" not in src

    def test_install_detect_regex_matches_install_commands(self):
        from kiso.brain.common import _INSTALL_CMD_RE
        assert _INSTALL_CMD_RE.search("kiso mcp install --from-url https://x")
        assert _INSTALL_CMD_RE.search("kiso skill install --from-url https://x")
        assert _INSTALL_CMD_RE.search("apt-get install curl")
        assert _INSTALL_CMD_RE.search("uv pip install flask")


class TestNoStrayToolSkillReferences:
    """: source must not contain stray **tool** refs as kiso concepts.

    v0.10 reintroduced "skill" as a first-class concept (skill_loader,
    skill_runtime, BRIEFER_SCHEMA.skills), so this check now only
    policies "tool" — the v0.9 rename target.

    Allowed exceptions:
    - [tool.uv], [tool.pytest], [tool.hatch] TOML sections (Python ecosystem)
    - User-facing regex patterns matching natural language in common.py
      (marked with M1320-allow comment)
    """

    EXCEPTIONS_PER_FILE_THRESHOLD = 30

    def _scan(self, paths: list[Path]) -> list[tuple[Path, int, str]]:
        hits: list[tuple[Path, int, str]] = []
        # v0.10: `skill` is a core concept — don't flag it. Only `tool` is
        # a v0.9 legacy word that should not reappear in source.
        word = re.compile(r"\b(tool|Tool|TOOL)\b")
        for base in paths:
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix not in {".py", ".md", ".json", ".sh"}:
                    continue
                if "__pycache__" in f.parts:
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if not word.search(line):
                        continue
                    if re.search(r"\[tool\.(uv|pytest|hatch|setuptools|poetry)", line):
                        continue
                    if "M1320-allow" in line:
                        continue
                    hits.append((f.relative_to(ROOT), lineno, line.strip()[:120]))
        return hits

    def test_kiso_source_clean(self):
        hits = self._scan([SRC])
        assert len(hits) <= self.EXCEPTIONS_PER_FILE_THRESHOLD, (
            f"kiso/ has {len(hits)} stray tool/skill refs:\n" +
            "\n".join(f"  {p}:{n}: {t}" for p, n, t in hits[:15])
        )

    def test_cli_source_clean(self):
        hits = self._scan([CLI])
        assert len(hits) <= self.EXCEPTIONS_PER_FILE_THRESHOLD, (
            f"cli/ has {len(hits)} stray tool/skill refs:\n" +
            "\n".join(f"  {p}:{n}: {t}" for p, n, t in hits[:15])
        )
