"""M1590 — lock test for `requires_mcp` marker application.

The marker (M1581) only helps if the tests that need a particular MCP
actually carry it. M1578-M1582 left two functional tests known to need
search-mcp without declaring the dependency: F40 (search→code→exec)
and F7 (search→synthesize→publish). M1590 applies the marker; this
lock asserts it stays applied so a future refactor doesn't silently
strip it and re-introduce the original "test runs against empty
catalog and fails for the wrong reason" failure mode.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_path,class_name,method_name,expected_mcp",
    [
        (
            "tests.functional.test_advanced_flows",
            "TestF40SearchCodeExec",
            "test_search_then_code_then_exec",
            "search-mcp",
        ),
        (
            "tests.functional.test_research",
            "TestF7ResearchAndPublish",
            "test_search_synthesize_publish",
            "search-mcp",
        ),
    ],
)
def test_requires_mcp_marker_present(
    module_path, class_name, method_name, expected_mcp,
):
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    method = getattr(cls, method_name)
    markers = list(getattr(method, "pytestmark", []))
    requires = [m for m in markers if m.name == "requires_mcp"]
    assert requires, (
        f"{module_path}::{class_name}::{method_name} missing "
        f"`@pytest.mark.requires_mcp(...)` — expected {expected_mcp!r}"
    )
    declared: set[str] = set()
    for marker in requires:
        if not marker.args:
            continue
        arg0 = marker.args[0]
        names = [arg0] if isinstance(arg0, str) else list(arg0)
        declared.update(names)
    assert expected_mcp in declared, (
        f"{module_path}::{class_name}::{method_name} declares "
        f"requires_mcp({sorted(declared)}) — expected {expected_mcp!r}"
    )
