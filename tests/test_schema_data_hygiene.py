"""M1577 — regression locks for fixture-data hygiene post-M1566.

After M1566 stripped the `wrapper` field / category / kind across
production code, several test fixtures still carried the retired
values (`category="wrapper"`, `entity_kind="wrapper"`). They passed
via mock paths but violated the live schema —
``_VALID_FACT_CATEGORIES = {"user", "project", "tool", "general"}``
and ``_ENTITY_KINDS`` (curator schema) without "wrapper".

This module locks the cleaned state. Allowed exception: files
that intentionally use `"wrapper"` as a *rejected* value in
negative tests (e.g. `tests/test_presets.py`'s
TestValidatePresetManifest cases) — those are verified separately
via an explicit allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TESTS = _REPO / "tests"

# Files intentionally containing the literal "wrapper" as a
# REJECTED value — they are negative tests verifying that the
# schema/CLI rejects it. These are NOT regressions.
_ALLOWED_FILES_WITH_WRAPPER_LITERAL = {
    "tests/test_presets.py",          # type="wrapper" rejected by manifest
    "tests/test_live_test_hygiene.py",  # M1572 lock — defensively forbids "wrapper="
    "tests/test_run_tests_sh_hygiene.py",  # M1573 lock — wrapper-vocab check
    "tests/test_phantom_skipif_audit.py",  # M1576 lock — references compose
    "tests/test_schema_data_hygiene.py",   # this file itself
    "tests/test_rename_completeness.py",   # rename completeness lock
    "tests/test_docs_retired_systems.py",  # docs retirement lock
}


def _iter_test_files() -> list[Path]:
    """Yield all .py files under tests/ that are not in the
    allowlist of intentional-wrapper-references."""
    out: list[Path] = []
    for path in _TESTS.rglob("*.py"):
        rel = path.relative_to(_REPO).as_posix()
        if rel in _ALLOWED_FILES_WITH_WRAPPER_LITERAL:
            continue
        if "__pycache__" in path.parts:
            continue
        out.append(path)
    return out


class TestNoStaleWrapperCategory:
    """No fixture data uses `category="wrapper"` — that value was
    retired in M1566 and is no longer in `_VALID_FACT_CATEGORIES`.
    Valid replacements: `"user"`, `"project"`, `"tool"`, `"general"`."""

    def test_no_category_wrapper_in_fixtures(self):
        offenders: list[tuple[str, int, str]] = []
        # Match `"category": "wrapper"` and `category="wrapper"`
        # (both common literal forms in fixtures).
        pattern = re.compile(
            r'(?:[\'"]category[\'"]?\s*[:=]\s*[\'"]wrapper[\'"])',
        )
        for path in _iter_test_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    rel = path.relative_to(_REPO).as_posix()
                    offenders.append((rel, i, line.strip()))
        assert not offenders, (
            f"these fixtures use `category=\"wrapper\"` (retired in "
            f"M1566): {offenders}"
        )


class TestNoStaleEntityKindWrapper:
    """No fixture data uses `entity_kind="wrapper"` — that value
    was retired from the curator's `_ENTITY_KINDS` in M1566. Valid
    replacements: see `kiso/brain/common.py` ENTITY_KINDS."""

    def test_no_entity_kind_wrapper_in_fixtures(self):
        offenders: list[tuple[str, int, str]] = []
        pattern = re.compile(
            r'(?:[\'"]entity_kind[\'"]?\s*[:=]\s*[\'"]wrapper[\'"])',
        )
        for path in _iter_test_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    rel = path.relative_to(_REPO).as_posix()
                    offenders.append((rel, i, line.strip()))
        assert not offenders, (
            f"these fixtures use `entity_kind=\"wrapper\"` (retired "
            f"in M1566): {offenders}"
        )


class TestNoWrapperCmdGroupParametrize:
    """test_prompts.py:150 used to parametrize cmd_groups with
    `"wrapper"`, but no `kiso wrapper ...` CLI exists post-M1504.
    The dead parametrize entry must be removed."""

    def test_test_prompts_no_wrapper_cmd_group(self):
        path = _TESTS / "test_prompts.py"
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        # We forbid an entry like `("wrapper", ...)` or `"wrapper"` at
        # the top level of a parametrize tuple. The simplest robust
        # pattern: line containing the literal `"wrapper"` inside a
        # cmd_group context. Use a contextual heuristic: a line in
        # this file that has `"wrapper"` AND is inside a parametrize
        # block is a hit.
        # We approximate by requiring the line to NOT also be in a
        # docstring or comment (lightweight check).
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if '"wrapper"' in stripped or "'wrapper'" in stripped:
                # Found a literal wrapper string. If it's in a
                # parametrize context (inside a list/tuple), fail.
                assert False, (
                    f"test_prompts.py line {i} contains `\"wrapper\"` "
                    f"which is a dead cmd_group post-M1504: {stripped}"
                )
