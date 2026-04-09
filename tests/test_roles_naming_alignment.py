"""Tests for M1293 — Roles naming alignment.

Two changes verified here:

1. Python function renames in ``kiso.brain``:
   - ``run_exec_translator`` → ``run_worker``
   - ``classify_message`` → ``run_classifier``
   - ``classify_inflight`` → ``run_inflight_classifier``

2. Bundled role file rename: ``summarizer-session.md`` → ``summarizer.md``,
   with an idempotent in-place migration for any existing user override.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Python function renames
# ---------------------------------------------------------------------------


class TestPythonFunctionRenames:
    """The new names exist; the old names do not."""

    def test_run_worker_exists_in_brain(self):
        from kiso import brain
        assert callable(brain.run_worker)

    def test_run_classifier_exists_in_brain(self):
        from kiso import brain
        assert callable(brain.run_classifier)

    def test_run_inflight_classifier_exists_in_brain(self):
        from kiso import brain
        assert callable(brain.run_inflight_classifier)

    def test_old_run_exec_translator_is_gone(self):
        from kiso import brain
        assert not hasattr(brain, "run_exec_translator"), (
            "run_exec_translator should be renamed to run_worker; "
            "no shim is allowed (M1293)"
        )

    def test_old_classify_message_is_gone(self):
        from kiso import brain
        assert not hasattr(brain, "classify_message"), (
            "classify_message should be renamed to run_classifier; "
            "no shim is allowed (M1293)"
        )

    def test_old_classify_inflight_is_gone(self):
        from kiso import brain
        assert not hasattr(brain, "classify_inflight"), (
            "classify_inflight should be renamed to run_inflight_classifier; "
            "no shim is allowed (M1293)"
        )


# ---------------------------------------------------------------------------
# Registry consistency after the rename
# ---------------------------------------------------------------------------


class TestRegistryAlignment:
    def test_summarizer_entry_uses_new_filename(self):
        from kiso.brain.roles_registry import get_role
        r = get_role("summarizer")
        assert r is not None
        assert r.prompt_filename == "summarizer.md"

    def test_summarizer_session_entry_is_gone(self):
        from kiso.brain.roles_registry import get_role
        assert get_role("summarizer-session") is None

    def test_worker_python_entry_uses_run_worker(self):
        from kiso.brain.roles_registry import get_role
        r = get_role("worker")
        assert r is not None
        assert r.python_entry.endswith("run_worker")

    def test_classifier_python_entry_uses_run_classifier(self):
        from kiso.brain.roles_registry import get_role
        r = get_role("classifier")
        assert r.python_entry.endswith("run_classifier")

    def test_inflight_classifier_python_entry_uses_run_inflight_classifier(self):
        from kiso.brain.roles_registry import get_role
        r = get_role("inflight-classifier")
        assert r.python_entry.endswith("run_inflight_classifier")


# ---------------------------------------------------------------------------
# Bundled file rename
# ---------------------------------------------------------------------------


class TestBundledFileRename:
    def test_bundle_has_summarizer_md_not_summarizer_session_md(self):
        bundle = Path(__file__).resolve().parent.parent / "kiso" / "roles"
        assert (bundle / "summarizer.md").is_file(), (
            "Bundled summarizer.md must exist after the rename"
        )
        assert not (bundle / "summarizer-session.md").exists(), (
            "Bundled summarizer-session.md should be removed after the rename"
        )

    def test_text_roles_loads_summarizer_not_summarizer_session(self):
        """The summarizer prompt loader uses the new role name."""
        import inspect
        from kiso.brain import text_roles
        src = inspect.getsource(text_roles)
        assert '"summarizer-session"' not in src
        assert "'summarizer-session'" not in src
        assert '"summarizer"' in src or "'summarizer'" in src


# ---------------------------------------------------------------------------
# In-place migration helper
# ---------------------------------------------------------------------------


class TestSummarizerMigration:
    def _make_user_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "roles"
        d.mkdir()
        return d

    def test_migration_renames_legacy_when_new_missing(self, tmp_path):
        from kiso.main import _migrate_summarizer_session_role
        d = self._make_user_dir(tmp_path)
        legacy = d / "summarizer-session.md"
        legacy.write_text("CUSTOM SUMMARIZER PROMPT", encoding="utf-8")
        _migrate_summarizer_session_role(d)
        assert not legacy.exists()
        assert (d / "summarizer.md").read_text() == "CUSTOM SUMMARIZER PROMPT"

    def test_migration_noop_when_only_new_present(self, tmp_path):
        from kiso.main import _migrate_summarizer_session_role
        d = self._make_user_dir(tmp_path)
        new = d / "summarizer.md"
        new.write_text("NEW PROMPT", encoding="utf-8")
        _migrate_summarizer_session_role(d)
        assert new.read_text() == "NEW PROMPT"

    def test_migration_keeps_both_when_both_present(self, tmp_path, caplog):
        from kiso.main import _migrate_summarizer_session_role
        d = self._make_user_dir(tmp_path)
        legacy = d / "summarizer-session.md"
        new = d / "summarizer.md"
        legacy.write_text("LEGACY", encoding="utf-8")
        new.write_text("NEW", encoding="utf-8")
        _migrate_summarizer_session_role(d)
        # Both files preserved; new one wins (is the canonical)
        assert legacy.exists()
        assert new.read_text() == "NEW"

    def test_migration_noop_when_neither_present(self, tmp_path):
        from kiso.main import _migrate_summarizer_session_role
        d = self._make_user_dir(tmp_path)
        _migrate_summarizer_session_role(d)
        assert list(d.iterdir()) == []

    def test_migration_idempotent(self, tmp_path):
        from kiso.main import _migrate_summarizer_session_role
        d = self._make_user_dir(tmp_path)
        legacy = d / "summarizer-session.md"
        legacy.write_text("CUSTOM", encoding="utf-8")
        _migrate_summarizer_session_role(d)
        _migrate_summarizer_session_role(d)
        assert (d / "summarizer.md").read_text() == "CUSTOM"


# ---------------------------------------------------------------------------
# Grep guard: no remaining references in kiso/ source tree
# ---------------------------------------------------------------------------


class TestGrepGuard:
    """Audit-time checks: old names must not appear in production source."""

    def _kiso_py_files(self) -> list[Path]:
        root = Path(__file__).resolve().parent.parent / "kiso"
        return list(root.rglob("*.py"))

    def test_no_run_exec_translator_in_kiso_source(self):
        for f in self._kiso_py_files():
            text = f.read_text(encoding="utf-8")
            assert "run_exec_translator" not in text, (
                f"{f}: still references run_exec_translator"
            )

    def test_no_classify_message_in_kiso_source(self):
        for f in self._kiso_py_files():
            text = f.read_text(encoding="utf-8")
            # `classify_message` is the old name; the new one is `run_classifier`.
            # Still allowed in docstrings? No — we want a hard rename.
            assert "classify_message" not in text, (
                f"{f}: still references classify_message"
            )

    def test_no_classify_inflight_in_kiso_source(self):
        for f in self._kiso_py_files():
            text = f.read_text(encoding="utf-8")
            assert "classify_inflight" not in text, (
                f"{f}: still references classify_inflight"
            )

    def test_no_summarizer_session_string_in_kiso_source_outside_migration(self):
        """`summarizer-session` only allowed in main.py (the migration code)."""
        root = Path(__file__).resolve().parent.parent / "kiso"
        for f in root.rglob("*.py"):
            if f.name == "main.py":
                continue  # migration helper is the only legitimate reference
            text = f.read_text(encoding="utf-8")
            assert "summarizer-session" not in text, (
                f"{f}: still references summarizer-session"
            )
