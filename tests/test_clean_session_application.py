"""M1598 — lock test for `clean_session` fixture application.

The `clean_session` fixture (M1582) yields an isolated KISO_DIR + fresh
DB + fresh session id, breaking the cross-test contamination introduced
by the session-scoped autouse `_func_kiso_dir`. The fixture is only
useful if the tests that need isolation actually request it. M1582
deferred the per-class roll-out across F1 / F2 / F7 / F40; M1598
applies it.

This lock asserts the marker stays applied to those four classes so a
future refactor doesn't silently strip it and re-introduce the
"F1 installed browser MCP leaks into F2 classifier" failure mode.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_path,class_name",
    [
        ("tests.functional.test_browser", "TestF1BrowserInstall"),
        ("tests.functional.test_browser", "TestF1BrowserNavigate"),
        ("tests.functional.test_browser", "TestF1BrowserScreenshot"),
        ("tests.functional.test_browser", "TestF2WikipediaPython"),
        ("tests.functional.test_research", "TestF7ResearchAndPublish"),
        ("tests.functional.test_advanced_flows", "TestF40SearchCodeExec"),
    ],
)
def test_clean_session_marker_present(module_path, class_name):
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    markers = list(getattr(cls, "pytestmark", []))
    usefixtures = [m for m in markers if m.name == "usefixtures"]
    declared: set[str] = set()
    for marker in usefixtures:
        declared.update(marker.args)
    assert "clean_session" in declared, (
        f"{module_path}::{class_name} missing "
        f"`@pytest.mark.usefixtures(\"clean_session\")` — required for "
        f"cross-test isolation per M1582/M1598. Declared usefixtures: "
        f"{sorted(declared) or '(none)'}"
    )
