"""M1572 / M1578 / M1594 — regression locks for test + production hygiene.

After Phase 4 / Phase 9 / Phase 13 retirements (wrapper subsystem,
connector plugin subsystem, registry.json), two live test files were
left behind that exclusively exercised the retired code paths. Their
imports fail, their fixtures error, and they pollute every full live
run with false-positive failures. This module asserts they stay gone,
plus that no live or functional test silently re-introduces patterns
that were removed in M1566 / M1545 (`wrapper=` kwarg, `registry_url`
fixture field, references to retired skills like `moltbook`, and
paradigm-mismatched vocabulary like "browser wrapper" / "OCR wrapper").

M1578 generalizes the M1572 lock from `tests/live/test_e2e.py` to ALL
files in `tests/live/*.py` and `tests/functional/*.py`, and adds the
paradigm-mismatch sweep.

M1594 extends the paradigm-mismatch sweep to production code under
`kiso/` and `cli/`, so any future regression of the retired wrapper
vocabulary in shipping code fails CI loudly.
"""

from __future__ import annotations

import re
from pathlib import Path


_TESTS_DIR = Path(__file__).resolve().parent
_LIVE_DIR = _TESTS_DIR / "live"
_FUNCTIONAL_DIR = _TESTS_DIR / "functional"
_CORE_DIR = _TESTS_DIR.parent
_PROD_KISO_DIR = _CORE_DIR / "kiso"
_PROD_CLI_DIR = _CORE_DIR / "cli"


def _live_and_functional_py_files() -> list[Path]:
    """Every `*.py` in tests/live/ and tests/functional/, excluding
    package files (`__init__.py`)."""
    files: list[Path] = []
    for d in (_LIVE_DIR, _FUNCTIONAL_DIR):
        for path in sorted(d.glob("*.py")):
            if path.name == "__init__.py":
                continue
            files.append(path)
    return files


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


class TestNoWrapperKwargInLiveAndFunctional:
    """`create_task()` (and the broader public store API) lost the
    `wrapper=` kwarg in M1566. M1572 locked it down for
    `tests/live/test_e2e.py`. M1578 generalizes the lock to every file
    in `tests/live/` and `tests/functional/`."""

    # Match `wrapper=` at start of a token (preceded by whitespace,
    # `(`, or `,`) — the kwarg pattern. Ignores substrings inside
    # longer identifiers and bare mentions in docstrings.
    KWARG_RE = re.compile(r"[\s(,]wrapper\s*=")

    def test_no_wrapper_kwarg_in_any_live_or_functional_file(self):
        offenders: list[str] = []
        for path in _live_and_functional_py_files():
            text = path.read_text()
            for match in self.KWARG_RE.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(_TESTS_DIR.parent)}:{line_no}")
        assert not offenders, (
            "live + functional tests must not use `wrapper=` as a "
            "keyword argument — that kwarg was removed in M1566. "
            f"Offenders: {offenders}"
        )


class TestNoParadigmMismatchedStrings:
    """Post-v0.10 vocabulary sweep. The wrapper subsystem was retired
    (M1504-M1566), `registry.json` was retired (M1545), and `moltbook`
    is a retired example skill. References to those concepts in live
    or functional tests are paradigm-mismatched: they either depend
    on dead fixture fields (`registry_url`) or describe behaviour using
    obsolete vocabulary ("browser wrapper", "OCR wrapper") that no
    longer matches what the planner / messenger actually produce.

    The lock asserts zero occurrences across `tests/live/*.py` and
    `tests/functional/*.py`.
    """

    PATTERNS: tuple[tuple[str, str], ...] = (
        ("registry_url", "registry.json was retired in M1545; field is dead in env-builder fixtures"),
        ("moltbook", "moltbook is a retired example skill"),
        ("browser wrapper", "wrapper subsystem retired in M1504-M1566; use 'browser MCP' instead"),
        ("OCR wrapper", "wrapper subsystem retired in M1504-M1566; use 'OCR MCP' instead"),
    )

    def test_no_paradigm_mismatched_strings(self):
        offenders: list[str] = []
        for path in _live_and_functional_py_files():
            text = path.read_text()
            for needle, _why in self.PATTERNS:
                start = 0
                while True:
                    idx = text.find(needle, start)
                    if idx < 0:
                        break
                    line_no = text[:idx].count("\n") + 1
                    rel = path.relative_to(_TESTS_DIR.parent)
                    offenders.append(f"{rel}:{line_no} `{needle}`")
                    start = idx + len(needle)
        if offenders:
            why_lines = "\n".join(f"  - `{n}`: {w}" for n, w in self.PATTERNS)
            offender_lines = "\n".join(f"  - {o}" for o in offenders)
            raise AssertionError(
                "live + functional tests must not reference "
                "paradigm-mismatched terms (post-v0.10 cleanup):\n"
                f"{why_lines}\n"
                f"Offenders:\n{offender_lines}"
            )


def _production_py_files() -> list[Path]:
    """Every `*.py` under `kiso/` and `cli/`, excluding caches."""
    files: list[Path] = []
    for d in (_PROD_KISO_DIR, _PROD_CLI_DIR):
        for path in sorted(d.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


class TestNoRetiredWrapperVocabInProduction:
    """M1594 — production-code hygiene lock.

    The wrapper subsystem was retired in M1504-M1566. The live + functional
    test tier already locks the retired vocabulary (M1578); this lock
    extends the same patterns to shipping code under `kiso/` and `cli/`,
    including comments and docstrings — once a phrase is paradigm-retired
    it should not survive anywhere a future contributor might copy from.
    """

    PATTERNS: tuple[tuple[str, str], ...] = (
        ("wrapper browser", "use 'MCP browser' (wrapper subsystem retired in M1504-M1566)"),
        ("browser wrapper", "use 'browser MCP' (wrapper subsystem retired in M1504-M1566)"),
        ("OCR wrapper", "use 'OCR MCP' (wrapper subsystem retired in M1504-M1566)"),
    )

    def test_no_retired_wrapper_vocab_in_kiso_or_cli(self):
        offenders: list[str] = []
        for path in _production_py_files():
            text = path.read_text(encoding="utf-8")
            for needle, _why in self.PATTERNS:
                start = 0
                while True:
                    idx = text.find(needle, start)
                    if idx < 0:
                        break
                    line_no = text[:idx].count("\n") + 1
                    rel = path.relative_to(_CORE_DIR)
                    offenders.append(f"{rel}:{line_no} `{needle}`")
                    start = idx + len(needle)
        if offenders:
            why_lines = "\n".join(f"  - `{n}`: {w}" for n, w in self.PATTERNS)
            offender_lines = "\n".join(f"  - {o}" for o in offenders)
            raise AssertionError(
                "production code under kiso/ and cli/ must not reference "
                "retired wrapper vocabulary (post-v0.10 cleanup):\n"
                f"{why_lines}\n"
                f"Offenders:\n{offender_lines}"
            )
