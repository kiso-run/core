"""Plugin test runner — clone, install, test official plugins from the registry.

Usage (from utils/run_tests.sh or directly):
    python -m cli.plugin_test_runner                    # all tools + connectors
    python -m cli.plugin_test_runner tools              # all tools
    python -m cli.plugin_test_runner connectors         # all connectors
    python -m cli.plugin_test_runner browser            # specific (auto-detect type)
    python -m cli.plugin_test_runner browser,discord    # multiple specific
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

OFFICIAL_ORG = "kiso-run"

# ANSI colors (matching utils/run_tests.sh)
_GREEN = "\033[0;32m"
_RED = "\033[0;31m"
_YELLOW = "\033[0;33m"
_DIM = "\033[2m"
_NC = "\033[0m"
_USE_COLOR = sys.stdout.isatty()
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "PATH": os.environ.get("PATH", ""),
}


@dataclass
class PluginTestResult:
    name: str
    plugin_type: str       # "tool" or "connector"
    stage: str             # "clone", "validate", "install", "test", "done"
    passed: bool = False
    skipped: bool = False
    error: str | None = None
    duration_s: float = 0.0
    test_count: int = 0


def _cprint(text: str, color: str) -> None:
    """Print with ANSI color if stdout is a TTY."""
    if _USE_COLOR:
        print(f"{color}{text}{_NC}")
    else:
        print(text)


def _git_url(plugin_type: str, name: str) -> str:
    prefix = "wrapper-" if plugin_type == "wrapper" else "connector-"
    return f"https://github.com/{OFFICIAL_ORG}/{prefix}{name}.git"


def _test_one_plugin(work_dir: Path, plugin_type: str, name: str) -> PluginTestResult:
    """Clone, install, and test a single plugin. Returns result with stage info."""
    start = time.monotonic()
    plugin_dir = work_dir / f"{plugin_type}-{name}"

    # --- Clone ---
    git_url = _git_url(plugin_type, name)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", git_url, str(plugin_dir)],
        capture_output=True, text=True, env=_GIT_ENV, timeout=60,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        return PluginTestResult(
            name=name, plugin_type=plugin_type, stage="clone",
            error=f"git clone failed: {stderr}",
            duration_s=time.monotonic() - start,
        )

    # --- Validate ---
    manifest = plugin_dir / "kiso.toml"
    if not manifest.exists():
        return PluginTestResult(
            name=name, plugin_type=plugin_type, stage="validate",
            error="missing kiso.toml",
            duration_s=time.monotonic() - start,
        )

    # --- Install (uv sync) ---
    result = subprocess.run(
        ["uv", "sync", "--group", "dev"],
        cwd=str(plugin_dir),
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Truncate long errors
        if len(stderr) > 300:
            stderr = stderr[:300] + "..."
        return PluginTestResult(
            name=name, plugin_type=plugin_type, stage="install",
            error=f"uv sync failed: {stderr}",
            duration_s=time.monotonic() - start,
        )

    # --- Check tests/ ---
    tests_dir = plugin_dir / "tests"
    if not tests_dir.is_dir() or not list(tests_dir.glob("test_*.py")):
        return PluginTestResult(
            name=name, plugin_type=plugin_type, stage="done",
            passed=True, skipped=True,
            error="no tests/ directory",
            duration_s=time.monotonic() - start,
        )

    # --- Run tests ---
    result = subprocess.run(
        ["uv", "run", "--group", "dev", "pytest", "tests/", "-v", "--tb=short"],
        cwd=str(plugin_dir),
        capture_output=True, text=True, timeout=180,
    )
    duration = time.monotonic() - start

    # Count tests from pytest output
    test_count = 0
    for line in result.stdout.splitlines():
        if " passed" in line or " failed" in line:
            # e.g. "5 passed, 1 failed in 2.3s"
            import re
            m = re.search(r"(\d+) passed", line)
            if m:
                test_count += int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                test_count += int(m.group(1))

    if result.returncode != 0:
        # Extract failure summary
        error_lines = []
        for line in result.stdout.splitlines():
            if "FAILED" in line or "ERROR" in line:
                error_lines.append(line.strip())
        error_msg = "\n".join(error_lines[:5]) if error_lines else "tests failed"
        return PluginTestResult(
            name=name, plugin_type=plugin_type, stage="test",
            error=error_msg, test_count=test_count,
            duration_s=duration,
        )

    return PluginTestResult(
        name=name, plugin_type=plugin_type, stage="done",
        passed=True, test_count=test_count,
        duration_s=duration,
    )


def _load_registry() -> dict:
    """Load registry from the repo root (no network needed)."""
    registry_path = Path(__file__).resolve().parents[1] / "registry.json"
    if registry_path.exists():
        return json.loads(registry_path.read_text())
    # Fallback: fetch from network
    from kiso.registry import fetch_registry
    return fetch_registry()


def _resolve_filter(registry: dict, filter_arg: str) -> list[tuple[str, str]]:
    """Parse filter arg into list of (plugin_type, name) tuples.

    filter_arg can be:
    - ""           → all tools + connectors
    - "tools"      → all tools
    - "connectors" → all connectors
    - "browser"    → auto-detect type from registry
    - "browser,discord" → multiple, auto-detect each
    """
    wrappers = registry.get("wrappers", [])
    connectors = registry.get("connectors", [])

    if not filter_arg:
        result = [("wrapper", t["name"]) for t in wrappers]
        result += [("connector", c["name"]) for c in connectors]
        return result

    if filter_arg in ("tools", "wrappers"):
        return [("wrapper", t["name"]) for t in wrappers]

    if filter_arg == "connectors":
        return [("connector", c["name"]) for c in connectors]

    # Specific names — auto-detect type
    wrapper_names = {t["name"] for t in wrappers}
    connector_names = {c["name"] for c in connectors}
    result = []
    for name in filter_arg.split(","):
        name = name.strip()
        if not name:
            continue
        if name in wrapper_names:
            result.append(("wrapper", name))
        elif name in connector_names:
            result.append(("connector", name))
        else:
            print(f"warning: '{name}' not found in registry, skipping", file=sys.stderr)
    return result


def test_plugins(
    filter_type: str | None = None,
    filter_names: list[str] | None = None,
) -> list[PluginTestResult]:
    """Test plugins from the registry. Returns list of results."""
    registry = _load_registry()
    filter_arg = ""
    if filter_type:
        filter_arg = filter_type
    elif filter_names:
        filter_arg = ",".join(filter_names)

    targets = _resolve_filter(registry, filter_arg)
    if not targets:
        print("No plugins to test.")
        return []

    results: list[PluginTestResult] = []
    with tempfile.TemporaryDirectory(prefix="kiso-plugin-test-") as tmp:
        work_dir = Path(tmp)
        for plugin_type, name in targets:
            label = f"{plugin_type}/{name}"
            print(f"\n  Testing {label}...", flush=True)
            r = _test_one_plugin(work_dir, plugin_type, name)
            results.append(r)
            # Inline status with colors
            if r.skipped:
                _cprint(f"  ⊘ {label}: skipped ({r.error})", _YELLOW)
            elif r.passed:
                _cprint(f"  ✓ {label}: {r.test_count} tests, {r.duration_s:.1f}s", _GREEN)
            else:
                _cprint(f"  ✗ {label}: failed at {r.stage} ({r.error})", _RED)

    _print_report(results)
    return results


def _print_report(results: list[PluginTestResult]) -> None:
    """Print final summary with total test count and elapsed time."""
    if not results:
        return

    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = sum(1 for r in results if not r.passed)
    skipped = sum(1 for r in results if r.skipped)
    total = len(results)
    total_tests = sum(r.test_count for r in results)
    total_time = sum(r.duration_s for r in results)

    parts = []
    if passed:
        parts.append(f"{_GREEN}{passed} passed{_NC}" if _USE_COLOR else f"{passed} passed")
    if failed:
        parts.append(f"{_RED}{failed} failed{_NC}" if _USE_COLOR else f"{failed} failed")
    if skipped:
        parts.append(f"{_YELLOW}{skipped} skipped{_NC}" if _USE_COLOR else f"{skipped} skipped")
    print(f"\n  Plugin Test Summary: {', '.join(parts)} (of {total})")
    print(f"  {total_tests} tests across {total} plugins in {total_time:.1f}s")


def main(filter_arg: str = "") -> int:
    """CLI entry point. Returns 0 if all passed, 1 if any failed."""
    results = test_plugins(
        filter_type=filter_arg if filter_arg in ("tools", "connectors") else None,
        filter_names=filter_arg.split(",") if filter_arg and filter_arg not in ("tools", "connectors") else None,
    )
    if not results:
        return 0
    return 0 if all(r.passed or r.skipped for r in results) else 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(main(arg))
