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
