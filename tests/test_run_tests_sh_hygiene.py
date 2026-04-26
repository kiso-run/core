"""M1573 — regression locks for `utils/run_tests.sh` hygiene.

The "Plugin tests" tier (menu choice 6) invoked
`cli.plugin_test_runner`, a module deleted in M1545. The tier itself
was conceptually retired by M1549 (per-repo CI on every
`kiso-run/*-mcp` repo replaced the centralized "test all plugins as
one batch" model). M1573 removes the tier across run_tests.sh,
docker-compose.test.yml, tests/README.md, and utils/test_times.json.
Menu choices 7/8/9 (Functional/Extended/Interactive) shift to 6/7/8.
"""

from __future__ import annotations

import json
from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent


class TestPluginTierGone:
    """No surface in the runner toolchain references the retired
    `cli.plugin_test_runner` module or the Plugin tier."""

    def test_run_tests_sh_no_plugin_test_runner_reference(self):
        text = (_REPO / "utils" / "run_tests.sh").read_text()
        assert "plugin_test_runner" not in text, (
            "utils/run_tests.sh must not reference the deleted "
            "cli.plugin_test_runner module"
        )

    def test_run_tests_sh_no_run_plugins_function(self):
        text = (_REPO / "utils" / "run_tests.sh").read_text()
        # The function definition `run_plugins() {` is the canonical
        # cue; also forbid any call sites.
        assert "run_plugins" not in text, (
            "utils/run_tests.sh must not define or call run_plugins"
        )

    def test_run_tests_sh_no_plugin_tier_menu_entry(self):
        text = (_REPO / "utils" / "run_tests.sh").read_text()
        # The menu line in interactive mode used to read
        # `${CYAN}6${NC}  Plugin tests   ...` — pin against
        # "Plugin tests" appearing as a tier name.
        assert "Plugin tests" not in text, (
            "utils/run_tests.sh must not advertise a 'Plugin tests' "
            "tier in the interactive menu"
        )

    def test_run_tests_sh_no_plugins_auto_flag(self):
        text = (_REPO / "utils" / "run_tests.sh").read_text()
        # `--plugins`, `--plugins=<filter>`, `--no-plugins` all retired.
        for flag in ("--plugins)", "--plugins=", "--no-plugins"):
            assert flag not in text, (
                f"utils/run_tests.sh must not advertise the {flag} "
                f"auto-mode flag"
            )

    def test_docker_compose_no_test_plugins_service(self):
        text = (_REPO / "docker-compose.test.yml").read_text()
        assert "test-plugins" not in text, (
            "docker-compose.test.yml must not declare a test-plugins "
            "service — it ran the deleted cli.plugin_test_runner"
        )
        assert "plugin_test_runner" not in text, (
            "docker-compose.test.yml must not reference "
            "cli.plugin_test_runner anywhere"
        )

    def test_tests_readme_no_plugin_tests_section(self):
        text = (_REPO / "tests" / "README.md").read_text()
        assert "## Plugin tests" not in text, (
            "tests/README.md must not document a 'Plugin tests' tier"
        )
        assert "plugin_test_runner" not in text, (
            "tests/README.md must not reference cli.plugin_test_runner"
        )

    def test_test_times_json_no_plugin_tests_key(self):
        path = _REPO / "utils" / "test_times.json"
        # The file may or may not exist (it is generated, not source).
        # If it exists, "Plugin tests" must not be a key.
        if not path.exists():
            return
        data = json.loads(path.read_text())
        assert "Plugin tests" not in data, (
            "utils/test_times.json must not retain a 'Plugin tests' "
            "key after the tier is retired"
        )


class TestRunnerMenuRenumbered:
    """After deleting the Plugin tier (was choice 6), menu entries
    7/8/9 shift down to 6/7/8. The shift is verified by static
    inspection of the menu-rendering block."""

    SCRIPT = _REPO / "utils" / "run_tests.sh"

    def test_choice_6_runs_functional(self):
        """In `_process_choices`, the case for `6)` must invoke
        run_functional (after the Plugin tier removal)."""
        text = self.SCRIPT.read_text()
        # Look for the case statement in _process_choices. The
        # canonical pattern is `6) run_functional ;;` — allow trailing
        # whitespace and arbitrary spacing.
        import re
        m = re.search(r"^\s*6\)\s*run_functional\s*;;", text, re.MULTILINE)
        assert m, (
            "_process_choices must map menu choice 6 to run_functional "
            "after the Plugin tier removal"
        )

    def test_choice_7_runs_extended(self):
        text = self.SCRIPT.read_text()
        import re
        m = re.search(r"^\s*7\)\s*run_extended\s*;;", text, re.MULTILINE)
        assert m, (
            "_process_choices must map menu choice 7 to run_extended"
        )

    def test_choice_8_runs_interactive(self):
        text = self.SCRIPT.read_text()
        import re
        m = re.search(r"^\s*8\)\s*run_interactive\s*;;", text, re.MULTILINE)
        assert m, (
            "_process_choices must map menu choice 8 to run_interactive"
        )

    def test_no_choice_9_in_process_choices(self):
        """The old choice 9 (Interactive) is now 8. Choice 9 should
        not exist in the case statement anymore."""
        text = self.SCRIPT.read_text()
        import re
        # Find the _process_choices function body and assert it does
        # not have a `9)` arm. We scope to the function body to avoid
        # false positives from elsewhere in the script.
        func_match = re.search(
            r"_process_choices\(\)\s*\{(.*?)\n\}",
            text, re.DOTALL,
        )
        assert func_match, "_process_choices function not found"
        body = func_match.group(1)
        assert not re.search(r"^\s*9\)", body, re.MULTILINE), (
            "_process_choices must not have a `9)` case after the "
            "Plugin tier removal — choices end at 8"
        )


class TestM1574NonPytestErrorRecap:
    """M1574 — when a suite fails before pytest produces output (e.g.
    `ModuleNotFoundError`, missing-module on import, compose-build
    error), `_extract_pytest_counts` must surface the error in the
    recap detail instead of leaving an uninformative `done` placeholder.
    """

    def _run_lib_mode(self, body: str) -> "subprocess.CompletedProcess":
        import subprocess
        return subprocess.run(
            ["bash", "-c", f"""
                export KISO_RUN_TESTS_LIB=1
                source ./utils/run_tests.sh
                {body}
            """],
            capture_output=True, text=True, timeout=10,
        )

    def test_run_tests_sh_lib_mode_guard_exists(self):
        """utils/run_tests.sh honors KISO_RUN_TESTS_LIB=1 and returns
        after defining functions, so unit tests can source and invoke
        helpers in isolation."""
        result = self._run_lib_mode('echo "OK"')
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout, (
            f"sourcing utils/run_tests.sh in lib mode should be a no-op; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_pytest_summary_wins_when_present(self):
        """When the log contains a pytest summary line, that is what
        _PYTEST_SUMMARY reports — the new fallback never overrides
        a real summary."""
        result = self._run_lib_mode("""
            tmp=$(mktemp)
            cat > "$tmp" <<'LOG'
ModuleNotFoundError: cli.something_old
3 passed in 1.20s
LOG
            _extract_pytest_counts "$tmp"
            echo "SUMMARY=$_PYTEST_SUMMARY"
            rm -f "$tmp"
        """)
        assert result.returncode == 0, result.stderr
        assert "SUMMARY=3 passed in 1.20s" in result.stdout, (
            f"pytest summary must win when present; "
            f"stdout={result.stdout!r}"
        )

    def test_module_not_found_surfaces_when_no_pytest_summary(self):
        """When no pytest summary is present, ModuleNotFoundError
        surfaces in _PYTEST_SUMMARY so the recap is informative."""
        result = self._run_lib_mode("""
            tmp=$(mktemp)
            cat > "$tmp" <<'LOG'
ImportError while loading conftest 'tests/conftest.py'.
ModuleNotFoundError: No module named 'cli.plugin_test_runner'
LOG
            _extract_pytest_counts "$tmp"
            echo "SUMMARY=$_PYTEST_SUMMARY"
            rm -f "$tmp"
        """)
        assert result.returncode == 0, result.stderr
        assert "ModuleNotFoundError" in result.stdout, (
            f"ModuleNotFoundError must surface in fallback summary; "
            f"stdout={result.stdout!r}"
        )

    def test_import_error_surfaces_when_no_pytest_summary(self):
        result = self._run_lib_mode("""
            tmp=$(mktemp)
            cat > "$tmp" <<'LOG'
some build noise...
ImportError: cannot import name 'foo' from 'bar' (/path/to/bar.py)
more noise
LOG
            _extract_pytest_counts "$tmp"
            echo "SUMMARY=$_PYTEST_SUMMARY"
            rm -f "$tmp"
        """)
        assert result.returncode == 0, result.stderr
        assert "ImportError" in result.stdout

    def test_summary_truncated_to_reasonable_length(self):
        """Fallback summary lines longer than ~120 chars are truncated
        so they do not blow up the recap row."""
        long_line = "Error: " + "x" * 200
        result = self._run_lib_mode(f"""
            tmp=$(mktemp)
            cat > "$tmp" <<'LOG'
{long_line}
LOG
            _extract_pytest_counts "$tmp"
            echo "SUMMARY=$_PYTEST_SUMMARY"
            echo "LEN=${{#_PYTEST_SUMMARY}}"
            rm -f "$tmp"
        """)
        assert result.returncode == 0, result.stderr
        # Find the LEN line and assert <= 130 (allow some slack for trailing ellipsis or similar).
        import re
        m = re.search(r"LEN=(\d+)", result.stdout)
        assert m, f"LEN line missing in stdout: {result.stdout!r}"
        length = int(m.group(1))
        assert length <= 130, (
            f"fallback summary should be truncated to ~120 chars, "
            f"got {length}"
        )

    def test_empty_log_yields_empty_summary(self):
        """An empty log produces an empty summary (consistent with
        the existing behavior — caller falls back to literal 'done')."""
        result = self._run_lib_mode("""
            tmp=$(mktemp)
            : > "$tmp"
            _extract_pytest_counts "$tmp"
            echo "SUMMARY=$_PYTEST_SUMMARY"
            echo "LEN=${#_PYTEST_SUMMARY}"
            rm -f "$tmp"
        """)
        assert result.returncode == 0, result.stderr
        assert "LEN=0" in result.stdout

    def test_no_match_yields_empty_summary(self):
        """A log with neither pytest output nor recognizable error
        patterns yields an empty summary — the caller's `done` default
        kicks in."""
        result = self._run_lib_mode("""
            tmp=$(mktemp)
            cat > "$tmp" <<'LOG'
Some random noise.
Building image...
Layer cached.
LOG
            _extract_pytest_counts "$tmp"
            echo "SUMMARY=$_PYTEST_SUMMARY"
            echo "LEN=${#_PYTEST_SUMMARY}"
            rm -f "$tmp"
        """)
        assert result.returncode == 0, result.stderr
        assert "LEN=0" in result.stdout
