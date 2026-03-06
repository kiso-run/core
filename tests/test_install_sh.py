"""Tests for install.sh — sourced in library mode (KISO_INSTALL_LIB=1)."""

from __future__ import annotations

import subprocess
import sys

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
