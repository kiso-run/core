"""Install-approval gate must recognise ``kiso mcp install`` and
``kiso skill install``.

Business requirement: when the planner proposes ``needs_install``,
the follow-up exec that eventually runs the install must be
gated by ``install_approved`` — whether the command is
``kiso connector install X``, ``kiso mcp install --from-url X``,
or ``kiso skill install --from-url X``. The same regex that catches
``apt-get install`` and ``kiso connector install`` must catch the
new two forms, or the worker's unapproved-install suppression is
silently bypassed.

On the flipside, once the user approves, the planner/worker both
allow these same commands to run.
"""

from __future__ import annotations

import re

import pytest

from kiso.brain import validate_plan
from kiso.brain.common import _INSTALL_CMD_RE


# ---------------------------------------------------------------------------
# Regex coverage
# ---------------------------------------------------------------------------


class TestInstallCmdRegex:
    def test_matches_kiso_connector_install(self):
        assert _INSTALL_CMD_RE.search("kiso connector install slack")

    def test_matches_kiso_mcp_install(self):
        assert _INSTALL_CMD_RE.search(
            "kiso mcp install --from-url https://github.com/acme/mcp"
        )

    def test_matches_kiso_skill_install(self):
        assert _INSTALL_CMD_RE.search(
            "kiso skill install --from-url https://github.com/acme/writing-style"
        )

    def test_matches_with_extra_whitespace(self):
        assert _INSTALL_CMD_RE.search("kiso  mcp   install   --from-url x")
        assert _INSTALL_CMD_RE.search("kiso skill\tinstall --from-url x")

    def test_does_not_match_benign_kiso_commands(self):
        assert not _INSTALL_CMD_RE.search("kiso mcp list")
        assert not _INSTALL_CMD_RE.search("kiso skill remove python-debug")
        assert not _INSTALL_CMD_RE.search("kiso skill info python-debug")
        assert not _INSTALL_CMD_RE.search("kiso skill test python-debug")


# ---------------------------------------------------------------------------
# validate_plan: mixed propose + install is still rejected
# ---------------------------------------------------------------------------


class TestValidatePlanMcpInstall:
    def test_mcp_install_in_first_plan_rejected(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso mcp install --from-url https://ex.com/mcp", "expect": "ok"},
            {"type": "msg", "detail": "Answer in English. report", "expect": None},
        ], "needs_install": ["npm:@acme/mcp"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_mcp_install_on_replan_allowed(self):
        plan = {"tasks": [
            {"type": "exec", "detail": "kiso mcp install --from-url https://ex.com/mcp", "expect": "ok"},
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True)
        assert not any("first plan" in e for e in errors)


class TestValidatePlanSkillInstall:
    def test_skill_install_in_first_plan_rejected(self):
        plan = {"tasks": [
            {
                "type": "exec",
                "detail": "kiso skill install --from-url https://github.com/acme/s",
                "expect": "ok",
            },
            {"type": "msg", "detail": "Answer in English. report", "expect": None},
        ], "needs_install": ["github.com/acme/s"]}
        errors = validate_plan(plan)
        assert any("first plan" in e for e in errors)

    def test_skill_install_on_replan_allowed(self):
        plan = {"tasks": [
            {
                "type": "exec",
                "detail": "kiso skill install --from-url https://github.com/acme/s",
                "expect": "ok",
            },
            {"type": "replan", "detail": "continue", "expect": None},
        ]}
        errors = validate_plan(plan, is_replan=True)
        assert not any("first plan" in e for e in errors)


class TestValidatePlanMsgOnlyProposalStillAccepted:
    """A needs_install proposal plan with a single msg task must
    still validate — this is the "ask before install" case the
    regex must not accidentally block."""

    def test_mcp_proposal_msg_only(self):
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. I need to install an MCP server.", "expect": None},
        ], "needs_install": ["npm:@acme/mcp"]}
        errors = validate_plan(plan)
        assert not any("first plan" in e for e in errors)

    def test_skill_proposal_msg_only(self):
        plan = {"tasks": [
            {"type": "msg", "detail": "Answer in English. I need to install a skill.", "expect": None},
        ], "needs_install": ["github.com/acme/s"]}
        errors = validate_plan(plan)
        assert not any("first plan" in e for e in errors)


# ---------------------------------------------------------------------------
# Worker suppression side — documentation test; the actual regex
# match is what the worker uses at kiso/worker/loop.py, so if the
# regex matches here, the suppression matches there.
# ---------------------------------------------------------------------------


class TestWorkerSuppressionCoverage:
    """Confirms that the regex the worker uses to decide whether a
    command is an install catches every command shape the planner
    can emit under the current install guidance."""

    PLANNER_EMITTABLE_INSTALL_COMMANDS = (
        "kiso mcp install --from-url https://github.com/acme/repo",
        "kiso skill install --from-url https://github.com/acme/skill",
        "kiso connector install slack",
        "uv pip install flask",
        "npx -y @modelcontextprotocol/server-github",
        "apt-get install curl",
    )

    @pytest.mark.parametrize("cmd", PLANNER_EMITTABLE_INSTALL_COMMANDS)
    def test_every_install_command_matches(self, cmd):
        assert _INSTALL_CMD_RE.search(cmd), (
            f"regex does not match {cmd!r} — worker would let it through"
            " unguarded"
        )
