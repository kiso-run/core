"""Tests for the `live_kiso_dir` fixture in `tests/live/conftest.py`.

Locks two contracts of the fixture:
- During a test that requests it, ``kiso.brain.KISO_DIR`` is bound
  to the test's tmp_path.
- After teardown, the original ``kiso.brain.KISO_DIR`` is restored
  (no leak across tests).

These run under the unit tier (no live LLM, no `--llm-live`
required) by exercising the fixture inside a self-contained
pytester subprocess. The subprocess gets the same project root
on its PYTHONPATH so it can resolve `kiso` and `tests.live.conftest`.
"""

from __future__ import annotations

from textwrap import dedent

import pytest


pytest_plugins = ["pytester"]


def _seed_project_root(pytester: pytest.Pytester) -> None:
    """Make the project root importable in the pytester subprocess
    so `kiso` and `tests.live.conftest` resolve."""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pytester.makepyfile(conftest=dedent(f'''
        import sys
        sys.path.insert(0, {root!r})
    '''))


def test_live_kiso_dir_rebinds_and_unwinds(pytester: pytest.Pytester) -> None:
    _seed_project_root(pytester)
    pytester.makepyfile(test_inner=dedent('''
        import pytest
        from tests.live.conftest import live_kiso_dir

        # Capture the pre-fixture KISO_DIR once at module import
        # time so the post-teardown test can verify the unwind.
        import kiso.brain
        _ORIGINAL_KISO_DIR = kiso.brain.KISO_DIR

        def test_rebinds_to_tmp_path(live_kiso_dir):
            import kiso.brain
            assert kiso.brain.KISO_DIR == live_kiso_dir
            assert kiso.brain.KISO_DIR != _ORIGINAL_KISO_DIR

        def test_unwinds_after_teardown():
            """Without requesting `live_kiso_dir`, brain.KISO_DIR
            must equal the pre-fixture original."""
            import kiso.brain
            assert kiso.brain.KISO_DIR == _ORIGINAL_KISO_DIR
    '''))
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=2)
