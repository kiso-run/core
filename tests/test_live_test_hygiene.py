"""M1572 — regression locks for live-test hygiene.

After Phase 4 / Phase 9 / Phase 13 retirements (wrapper subsystem,
connector plugin subsystem, registry.json), two live test files were
left behind that exclusively exercised the retired code paths. Their
imports fail, their fixtures error, and they pollute every full live
run with 8 + 5 = 13 false-positive failures. This module asserts they
stay gone, plus that `tests/live/test_e2e.py` does not silently
re-introduce the `wrapper=` kwarg that was removed from
`create_task()` in M1566.
"""

from __future__ import annotations

from pathlib import Path


_LIVE_DIR = Path(__file__).resolve().parent / "live"


class TestZombieFilesGone:
    """The two retired-subsystem live test files must not exist."""

    def test_test_cli_live_py_deleted(self):
        """tests/live/test_cli_live.py imported `cli.wrapper` (deleted
        in M1504) and `cli.connector._connector_install/_search`
        (deleted in M1525). Every class in the file failed; nothing
        recoverable. M1572 deletes the file outright — re-introducing
        it would resurrect 8 failing tests."""
        assert not (_LIVE_DIR / "test_cli_live.py").exists(), (
            "tests/live/test_cli_live.py must stay deleted — its 6 "
            "classes test wrapper/connector subsystems retired in "
            "M1504 / M1525"
        )

    def test_test_plugins_py_deleted(self):
        """tests/live/test_plugins.py loaded `registry.json` via
        `Path(...).read_text()`, but `registry.json` was deleted in
        M1545. Every test errored at fixture setup. M1572 deletes
        the file."""
        assert not (_LIVE_DIR / "test_plugins.py").exists(), (
            "tests/live/test_plugins.py must stay deleted — it depends "
            "on registry.json which was retired in M1545"
        )


class TestE2ENoStaleWrapperKwarg:
    """tests/live/test_e2e.py:59 used to call `create_task(...,
    wrapper=t.get('wrapper'), ...)`. The `wrapper` kwarg was removed
    from `create_task()` in M1566. M1572 dropped the line; this lock
    catches accidental re-introduction."""

    E2E_FILE = _LIVE_DIR / "test_e2e.py"

    def test_no_wrapper_kwarg_in_create_task_call(self):
        text = self.E2E_FILE.read_text()
        # The full broken pattern was `wrapper=t.get("wrapper")`. Pin
        # the literal so a different context using `wrapper=` (e.g. a
        # comment or docstring) does not accidentally lock too tight.
        assert "wrapper=t.get(" not in text, (
            "tests/live/test_e2e.py must not pass a `wrapper=` kwarg "
            "to create_task() — that kwarg was removed in M1566"
        )

    def test_no_bare_wrapper_kwarg_anywhere(self):
        """Defense in depth: catch other `wrapper=...` keyword forms
        (in case a future edit re-introduces the kwarg via a
        differently-named source variable)."""
        text = self.E2E_FILE.read_text()
        # We allow the substring `wrapper` to appear in comments or
        # docstrings — search only for the kwarg pattern, i.e. `wrapper=`
        # NOT preceded by another character that would make it part of
        # a longer identifier.
        import re
        # Match `wrapper=` at start of token (preceded by whitespace,
        # `(`, or `,`).
        kwarg_uses = re.findall(r"[\s(,]wrapper\s*=", text)
        assert not kwarg_uses, (
            f"tests/live/test_e2e.py must not use `wrapper=...` as a "
            f"keyword argument; found {len(kwarg_uses)} occurrences"
        )
