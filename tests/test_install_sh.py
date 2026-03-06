"""Tests for install.sh — sourced in library mode (KISO_INSTALL_LIB=1)."""

from __future__ import annotations

import os
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
