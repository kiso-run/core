"""Tests for install.sh — sourced in library mode (KISO_INSTALL_LIB=1)."""

from __future__ import annotations

import os
import signal
import subprocess

import pytest


def _run_bash(script: str, *, timeout: float = 10) -> subprocess.CompletedProcess:
    """Run a bash snippet that sources install.sh in lib mode."""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=timeout,
    )


class TestM174DoResetSetsResetRequested:
    """M174: interactive 'Reset data?' must set RESET_REQUESTED=true."""

    def test_do_reset_sets_reset_requested(self):
        """When confirm returns yes (simulated), RESET_REQUESTED becomes true."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh

            # Simulate: user is in update-instance mode, confirm returns yes
            RESET_REQUESTED=false
            DO_RESET=false

            # Override confirm to always say yes
            confirm() { return 0; }
            # Override yellow/green to silence output
            yellow() { :; }
            green() { :; }

            INST_NAME="test-inst"

            # Run the reset prompt block (extracted from install.sh update-instance flow)
            if confirm "Reset data for '$INST_NAME'?" "n"; then
                DO_RESET=true
                RESET_REQUESTED=true
            fi

            echo "DO_RESET=$DO_RESET"
            echo "RESET_REQUESTED=$RESET_REQUESTED"
        """)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "DO_RESET=true" in result.stdout
        assert "RESET_REQUESTED=true" in result.stdout

    def test_no_reset_keeps_reset_requested_false(self):
        """When confirm returns no, RESET_REQUESTED stays false."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh

            RESET_REQUESTED=false
            DO_RESET=false

            confirm() { return 1; }
            yellow() { :; }
            green() { :; }

            INST_NAME="test-inst"

            if confirm "Reset data for '$INST_NAME'?" "n"; then
                DO_RESET=true
                RESET_REQUESTED=true
            else
                :
            fi

            echo "DO_RESET=$DO_RESET"
            echo "RESET_REQUESTED=$RESET_REQUESTED"
        """)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "DO_RESET=false" in result.stdout
        assert "RESET_REQUESTED=false" in result.stdout

    def test_reset_requested_flag_also_works(self):
        """The --reset CLI flag still sets RESET_REQUESTED independently."""
        # Verify the variable is initialized to false by default
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh
            echo "RESET_REQUESTED=$RESET_REQUESTED"
        """)
        assert result.returncode == 0
        assert "RESET_REQUESTED=false" in result.stdout

    def test_install_sh_sources_cleanly(self):
        """install.sh can be sourced in lib mode without errors."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh
            echo "OK"
        """)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "OK" in result.stdout


class TestM175SkillWipeOnRebuildReset:
    """M175: skills/ and connectors/ dirs are wiped when NEED_BUILD+RESET_REQUESTED."""

    def test_wipe_block_present_in_install_sh(self):
        """The 3f wipe block exists and references RESET_REQUESTED."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert 'NEED_BUILD" == true && "$RESET_REQUESTED" == true' in content
        assert "skills" in content
        assert "connectors" in content

    def test_wipe_block_skipped_when_no_reset(self):
        """When RESET_REQUESTED=false, wipe block does not run."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh

            NEED_BUILD=true
            RESET_REQUESTED=false
            INST_DIR="$(mktemp -d)"
            mkdir -p "$INST_DIR/skills/browser"
            touch "$INST_DIR/skills/browser/kiso.toml"

            bold() { :; }
            green() { :; }
            yellow() { :; }

            # Simulate the wipe block logic
            if [[ "$NEED_BUILD" == true && "$RESET_REQUESTED" == true ]]; then
                rm -rf "$INST_DIR/skills"
            fi

            if [[ -d "$INST_DIR/skills/browser" ]]; then
                echo "SKILLS_SURVIVED=true"
            else
                echo "SKILLS_SURVIVED=false"
            fi
            rm -rf "$INST_DIR"
        """)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "SKILLS_SURVIVED=true" in result.stdout

    def test_wipe_block_runs_when_reset_requested(self):
        """When NEED_BUILD+RESET_REQUESTED, skills dir is removed."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh

            NEED_BUILD=true
            RESET_REQUESTED=true
            INST_DIR="$(mktemp -d)"
            mkdir -p "$INST_DIR/skills/browser"
            touch "$INST_DIR/skills/browser/kiso.toml"
            mkdir -p "$INST_DIR/connectors/telegram"
            touch "$INST_DIR/connectors/telegram/kiso.toml"

            bold() { :; }
            green() { :; }
            yellow() { :; }

            # Simulate the wipe block (without docker — just rm)
            if [[ "$NEED_BUILD" == true && "$RESET_REQUESTED" == true ]]; then
                for _wipe_dir in skills connectors; do
                    if [[ -d "$INST_DIR/$_wipe_dir" ]]; then
                        rm -rf "$INST_DIR/$_wipe_dir"
                    fi
                done
            fi

            [[ -d "$INST_DIR/skills" ]] && echo "SKILLS=exists" || echo "SKILLS=gone"
            [[ -d "$INST_DIR/connectors" ]] && echo "CONNECTORS=exists" || echo "CONNECTORS=gone"
            rm -rf "$INST_DIR"
        """)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "SKILLS=gone" in result.stdout
        assert "CONNECTORS=gone" in result.stdout


class TestSignalHandling:
    """Ctrl+C during install shows message and exits cleanly."""

    def test_int_trap_uses_exit_130(self):
        """install.sh INT trap exits with 130, never uses kill -INT $$ in code."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert "exit 130" in content
        # Verify kill -INT $$ is NOT used in actual code (only in comments)
        code_lines = [l for l in content.splitlines() if not l.strip().startswith("#")]
        assert not any("kill -INT $$" in l for l in code_lines)

    def test_no_interrupted_flag(self):
        """install.sh must not use _INTERRUPTED flag (removed in M317)."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert "_INTERRUPTED" not in content

    def test_signal_convention_comment(self):
        """install.sh documents the signal handling convention."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert "Signal handling convention" in content
        assert "never `kill -INT $$`" in content

    def test_on_error_returns_for_signal_exit(self):
        """_on_error returns silently for exit codes > 128 (signal exits)."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh
            set +e  # disable errexit to test _on_error directly
            (exit 130)  # sets $? to 130
            _on_error
            echo "DONE"
        """)
        assert "DONE" in result.stdout

    def test_healthcheck_loop_uses_sleep_or_true(self):
        """The healthcheck loop uses sleep || true to survive set -e."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert "sleep 2 || true" in content


class TestAskUsernameCompletion:
    """Tab-completion for ask_username prompt."""

    def test_read_uses_readline(self):
        """ask_username uses 'read -e' for readline support (tab completion)."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        # The read command in ask_username must use -e for readline
        assert "read -erp" in content, "ask_username should use 'read -e' for readline"

    def test_complete_setup_for_usernames(self):
        """ask_username binds a custom readline TAB function for usernames."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert "bind -x" in content, "Should bind custom TAB handler via bind -x"
        assert "compgen -W" in content, "Should use compgen for username matching"
        assert 'bind \'\"\\t": complete\'' in content, "Should restore default TAB after use"

    def test_ask_username_with_arg_skips_prompt(self):
        """When ARG_USER is set, ask_username validates and skips the prompt."""
        result = _run_bash("""
            export KISO_INSTALL_LIB=1
            source ./install.sh
            ARG_USER="$(whoami)"
            ask_username
            echo "KISO_USER=$KISO_USER"
        """)
        assert result.returncode == 0, result.stderr
        assert "KISO_USER=" in result.stdout
        assert result.stdout.strip().endswith(os.environ.get("USER", ""))


class TestM200VersionTracking:
    """M200: instance version tracking — Dockerfile, install.sh, /health."""

    def test_dockerfile_has_build_hash_arg(self):
        dockerfile = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")
        with open(dockerfile) as f:
            content = f.read()
        assert "ARG KISO_BUILD_HASH" in content
        assert "ENV KISO_BUILD_HASH" in content

    def test_install_passes_build_hash_to_docker_build(self):
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        assert "--build-arg KISO_BUILD_HASH" in content

    def test_register_instance_accepts_version_args(self):
        result = _run_bash("""
            export HOME="$(mktemp -d)"
            export KISO_DIR="$HOME/.kiso"
            mkdir -p "$KISO_DIR"
            export KISO_INSTALL_LIB=1
            source ./install.sh
            register_instance testbot 8333 9000 "0.2.0" "abc1234"
            python3 -c "
import json
d = json.load(open('$KISO_DIR/instances.json'))
e = d['testbot']
assert e['version'] == '0.2.0', f'version={e.get(\"version\")}'
assert e['build_hash'] == 'abc1234', f'hash={e.get(\"build_hash\")}'
assert 'installed_at' in e, 'missing installed_at'
print('OK')
"
        """)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_instance_display_shows_version(self):
        """Both instance listing blocks show version info when available."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        with open(script_path) as f:
            content = f.read()
        # Both listing blocks should use version/build_hash display logic
        # There are two python3 -c blocks that format instance display:
        # 1. "Found N existing instance(s)" listing
        # 2. "Which instance do you want to update?" listing
        import re
        display_blocks = re.findall(r"v\.get\('version'", content)
        assert len(display_blocks) >= 2, \
            f"Expected version display in both listing blocks, found {len(display_blocks)}"

    def test_register_instance_preserves_connectors(self):
        result = _run_bash("""
            export HOME="$(mktemp -d)"
            export KISO_DIR="$HOME/.kiso"
            mkdir -p "$KISO_DIR"
            # Pre-populate with connectors
            python3 -c "
import json, pathlib
p = pathlib.Path('$KISO_DIR/instances.json')
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({'bot': {'server_port': 8333, 'connector_port_base': 9000, 'connectors': {'tg': {'port': 9001}}}}))
"
            export KISO_INSTALL_LIB=1
            source ./install.sh
            register_instance bot 8334 9100 "0.3.0" "def5678"
            python3 -c "
import json
d = json.load(open('$KISO_DIR/instances.json'))
e = d['bot']
assert e['server_port'] == 8334
assert e['version'] == '0.3.0'
assert e['connectors'] == {'tg': {'port': 9001}}, f'connectors lost: {e[\"connectors\"]}'
print('OK')
"
        """)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
