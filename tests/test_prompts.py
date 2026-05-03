"""Prompt smoke tests.

Lightweight guards that role prompt files exist, load, and contain core
terms.  Behavioral validation lives in test_brain.py and test_worker.py —
these only catch accidental file deletion or catastrophic rewrites.

Also includes coarse prompt-budget tests merged from test_timeout_prompts.py.
"""

from pathlib import Path

import pytest

from kiso.brain import _BRIEFER_MODULE_DESCRIPTIONS, _load_modular_prompt

_ROLES_DIR = Path(__file__).resolve().parent.parent / "kiso" / "roles"

_EXPECTED_ROLES = [
    "planner.md", "reviewer.md", "messenger.md", "worker.md",
    "classifier.md", "briefer.md", "curator.md",
    "summarizer.md", "paraphraser.md",
    "inflight-classifier.md",
]

# Core terms per role — if these disappear the LLM behavior will break.
# Only include terms that are structurally essential, not cosmetic phrasing.
_ROLE_CORE_TERMS: list[tuple[str, list[str]]] = [
    ("planner.md", ["json plan", "goal", "tasks", "msg", "replan"]),
    ("reviewer.md", ["ok", "replan", "stuck", "expect", "summary", "deterministic"]),
    ("messenger.md", ["language", "verbatim", "fabricat", "published files", "digits"]),
    ("worker.md", ["cannot_translate", "sudo"]),
    ("classifier.md", ["plan", "chat"]),
    ("briefer.md", ["context", "modules"]),
    ("curator.md", ["promote", "discard", "entity"]),
    ("summarizer.md", ["english"]),
    ("paraphraser.md", ["paraphras"]),
    ("inflight-classifier.md", ["stop", "update", "independent", "conflict"]),
]


@pytest.mark.parametrize("filename", _EXPECTED_ROLES)
def test_role_file_exists_and_nonempty(filename):
    path = _ROLES_DIR / filename
    assert path.is_file(), f"Missing role file: {filename}"
    assert len(path.read_text().strip()) > 20, f"Role file too short: {filename}"


# ---------------------------------------------------------------------------
# M1610 — reviewer prompt invariants (abstract — no specific commands)
# ---------------------------------------------------------------------------


def test_reviewer_treats_exit_code_as_primary_signal():
    """The reviewer prompt must mark the exit code as the PRIMARY
    success signal for action tasks. The wording must explicitly
    state that exit=0 (with no error in stderr) is success even
    when stdout is empty / silent — many commands (installers,
    system tools, side-effecting scripts) print nothing on success.

    Without this, the reviewer over-replans on `exit=0 stdout=""`
    cases (M1610 motivation: F17 Playwright/Chromium pipeline tripped
    the circular-replan detector after the reviewer kept asking to
    replan a successful `playwright install chromium`).

    No specific command name (Playwright, Chromium, etc.) may appear
    in the new wording — the rule is general.
    """
    text = (_ROLES_DIR / "reviewer.md").read_text().lower()
    # The directive must pair "exit" + "primary" / "primary signal"
    # AND explicitly accept silent / empty stdout as compatible with
    # success on exit=0.
    has_primary = (
        ("exit code" in text or "exit=0" in text or "exit 0" in text)
        and ("primary" in text)
    )
    has_silent_ok = (
        "silent" in text
        or "empty stdout" in text
        or "no output" in text
        or "stdout is empty" in text
    )
    assert has_primary and has_silent_ok, (
        "reviewer.md must (a) name the exit code as the primary signal "
        "of action-task success and (b) explicitly accept exit=0 with "
        "silent/empty stdout — without this the reviewer over-replans "
        "on commands that succeed quietly"
    )


def test_reviewer_replan_requires_concrete_failure_signal():
    """The reviewer prompt must state that ``replan`` requires a
    concrete failure signal — exit≠0, explicit stderr error, or
    output that *contradicts* the expect — and that "stdout doesn't
    mention the command name" alone is NOT a failure signal.

    Abstract: no specific command / tool name appears in the new
    wording.
    """
    text = (_ROLES_DIR / "reviewer.md").read_text().lower()
    # Must contain "contradict" (output contradicts expect — the
    # narrow valid replan trigger) AND a phrase that rejects the
    # over-zealous case (stdout doesn't mention the command name
    # is not a failure).
    has_contradict = "contradict" in text
    has_silence_not_failure = (
        "not a failure" in text
        or "is acceptable" in text
        or "is fine" in text
    )
    assert has_contradict and has_silence_not_failure, (
        "reviewer.md must keep `replan` narrow: only on concrete "
        "failure (exit non-zero, stderr error, output contradicting "
        "expect) — silence-on-success is not a failure signal"
    )


@pytest.mark.parametrize(
    "filename,terms", _ROLE_CORE_TERMS,
    ids=[t[0].replace(".md", "") for t in _ROLE_CORE_TERMS],
)
def test_role_prompt_has_core_terms(filename, terms):
    content = (_ROLES_DIR / filename).read_text().lower()
    for term in terms:
        assert term in content, f"{filename}: missing core term '{term}'"


class TestWorkerSudoRule:
    """worker prompt has sudo-stripping rule for root."""

    def test_worker_prompt_has_root_sudo_rule(self):
        content = (_ROLES_DIR / "worker.md").read_text()
        lower = content.lower()
        assert "sudo" in lower
        assert "root" in lower

    def test_worker_prompt_has_cannot_translate(self):
        content = (_ROLES_DIR / "worker.md").read_text()
        assert "CANNOT_TRANSLATE" in content

class TestPlannerSearchPreference:
    """planner web guidance keeps built-in search as the default."""

    def test_web_module_describes_search_routing(self):
        web_module = _load_modular_prompt("planner", ["web"]).lower()
        assert "search" in web_module
        assert "browser" in web_module
        assert "finding information" in web_module


class TestPromptBudgetSmoke:
    """Prompt files must stay within coarse size budgets."""

    @pytest.mark.parametrize("filename,max_chars", [
        # planner.md budget includes the opt-in `investigate` module
        # and the `mcp` module (added as part of MCP consumer client
        # integration). Both are only loaded into the assembled prompt
        # when the relevant flag is set or the briefer opts in. The
        # actual loaded prompt for default plan/chat/chat_kb paths is
        # smaller because the modular loader excludes non-selected
        # sections. The MCP Prompts routing paragraph in
        # `skills_and_mcp`, the chat-mediated install trust-surface
        # note, and the new `mcp_recovery` module (opt-in, only
        # loaded when servers are flagged unhealthy) push the
        # worst-case size up accordingly.
        # M1579c (2026-04-28): bumped from 18000 → 20500 to accommodate
        # the new "Capability missing — ask-first flow" block + the
        # FORBIDDEN behaviors block (broker model anti-overfitting).
        # M1608 (2026-05-03): bumped from 20500 → 22200 to accommodate
        # the "DECISION TREE" block at the top of planning_rules (an
        # ordered if/elif/elif/else routing of plan shapes, replacing
        # the parallel rules that were causing V4-Flash flake), one
        # abstract example, the corresponding FORBIDDEN entries, the
        # trust-default rule (untrusted is the default tier for any
        # source not on a tier1 allowlist), and the disambiguation of
        # the "installs are immediate" rule into the precise
        # `install_approved` conditional that no longer contradicts
        # Decision Tree branch 1.
        # M1609 (2026-05-03): bumped to 23700 to accommodate the
        # MCP-vs-exec capability rule (prefer installed MCP over
        # inline-exec reimplementation when MCP declares matching
        # capability) + abstract examples + unhealthy-fallback clause.
        ("planner.md", 23700),
        ("messenger.md", 2500),
        # M1610 (2026-05-03): bumped from 3400 → 3900 to accommodate
        # the "exit code is the primary signal" rule (exit=0 + silent
        # stdout = ok, replan requires concrete failure signal —
        # contradiction, exit≠0, or stderr error). Replaces the
        # over-zealous "Empty output → replan" generic rule.
        ("reviewer.md", 3900),
    ])
    def test_prompt_size(self, filename, max_chars):
        size = len((_ROLES_DIR / filename).read_text())
        assert size < max_chars, f"{filename}: {size} chars exceeds {max_chars}"


class TestPlannerModules:
    """All planner modules must be individually loadable."""

    _ALL_MODULES = [
        "core", "planning_rules", "skills_and_mcp",
        "data_flow", "web", "replan",
        "kiso_commands", "user_mgmt", "plugin_install",
        "mcp_recovery", "session_files", "investigate",
    ]

    @pytest.mark.parametrize("module", [m for m in _ALL_MODULES if m != "core"])
    def test_module_loads(self, module):
        result = _load_modular_prompt("planner", [module])
        # Each named module must contribute its own content beyond `core`.
        # Compare against a core-only baseline so a renamed/removed module
        # cannot silently pass with just the core text.
        core_only = _load_modular_prompt("planner", [])
        assert len(result) > len(core_only), (
            f"Module {module!r} did not contribute any content beyond core "
            f"({len(result)} == {len(core_only)} chars). "
            f"Either the module was renamed/removed or planner.md no longer "
            f"defines it."
        )


class TestCLICommandAudit:
    """Planner kiso_commands module must match actual CLI subcommands."""

    def _get_actual_subcommands(self):
        import argparse as _argparse
        from cli import build_parser
        parser = build_parser()
        result = {}
        for action in parser._subparsers._actions:
            if not isinstance(action, _argparse._SubParsersAction):
                continue
            for name, subparser in action.choices.items():
                subs = []
                if subparser._subparsers is not None:
                    for sub_action in subparser._subparsers._actions:
                        if isinstance(sub_action, _argparse._SubParsersAction):
                            subs = list(sub_action.choices.keys())
                result[name] = sorted(subs)
        return result

    @pytest.mark.parametrize("cmd_group", ["connector", "env", "user", "reset"])
    def test_subcommands_match(self, cmd_group):
        from kiso.brain import _load_modular_prompt
        prompt = _load_modular_prompt("planner", ["kiso_commands"])
        actual = self._get_actual_subcommands()
        if cmd_group in actual:
            for sub in actual[cmd_group]:
                assert sub in prompt, f"Missing {cmd_group} subcommand: {sub}"


class TestLearningPipeline:
    """Schema + clean_learn_items survive prompt edits."""

    def test_reviewer_schema_max_3_learns(self):
        from kiso.brain import REVIEW_SCHEMA
        learn_schema = REVIEW_SCHEMA["json_schema"]["schema"]["properties"]["learn"]
        assert learn_schema["anyOf"][0]["maxItems"] == 3

    def test_clean_learn_items_filters_garbage(self):
        from kiso.brain import clean_learn_items
        bad_batch = [
            "browser wrapper installed successfully",
            "The contact form includes Name [8], Email [9], and details [10].",
            "guidance.studio has a contact form with name and email fields",
        ]
        cleaned = clean_learn_items(bad_batch)
        assert len(cleaned) <= 3
        assert any("guidance.studio" in item for item in cleaned)
        assert not any("installed successfully" in item for item in cleaned)


class TestTimeoutUnification:
    """per-role timeouts removed, single llm_timeout used."""

    def test_default_config_has_no_per_role_timeouts(self):
        from kiso.config import SETTINGS_DEFAULTS
        assert "planner_timeout" not in SETTINGS_DEFAULTS
        assert "messenger_timeout" not in SETTINGS_DEFAULTS
        assert "llm_timeout" in SETTINGS_DEFAULTS


class TestBrieferModuleDescriptions:
    """briefer module descriptions must be <=60 chars each."""

    def test_all_descriptions_within_limit(self):
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert len(desc) <= 60, (
                f"Module '{name}' description is {len(desc)} chars (max 60): {desc!r}"
            )

    def test_descriptions_are_nonempty(self):
        for name, desc in _BRIEFER_MODULE_DESCRIPTIONS.items():
            assert desc.strip(), f"Module '{name}' has empty description"

    def test_replan_description_no_self_reference(self):
        """'replan' description must not contain the word 'replan' to avoid
        LLM hallucination (e.g. 'replen') caused by token-level repetition."""
        desc = _BRIEFER_MODULE_DESCRIPTIONS["replan"]
        assert "replan" not in desc.lower(), (
            f"Module 'replan' description should not repeat its own name: {desc!r}"
        )
