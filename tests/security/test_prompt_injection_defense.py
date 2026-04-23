"""Concern 10 — untrusted content passes through the paraphraser.

Two surfaces need the defence:
- Untrusted inbound messages (routed through ``/sessions/.../untrusted``)
  must be rewritten by the paraphraser before the planner sees them.
- The messenger's skill-sections pathway must label the skill sections
  as style guidance, not as trusted command input. The planner's
  routing cannot take orders hidden inside a skill body.

This test pins the structural invariants:
- The paraphraser role exists and its prompt instructs the LLM to
  rewrite untrusted content as third-person factual summaries.
- The worker loop imports and references ``run_paraphraser`` on the
  path that handles untrusted messages.
- The messenger's skill injection labels the block as output style,
  not as authoritative task instructions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiso.brain import text_roles as text_roles_mod


ROLES_DIR = Path(text_roles_mod.__file__).resolve().parent.parent / "roles"
WORKER_LOOP = (
    Path(text_roles_mod.__file__).resolve().parent.parent / "worker" / "loop.py"
)


class TestParaphraserRole:
    def test_paraphraser_prompt_states_defence_purpose(self):
        prompt = (ROLES_DIR / "paraphraser.md").read_text(encoding="utf-8")
        assert "untrusted" in prompt.lower()
        # The prompt must force a summary form — not a verbatim echo.
        assert (
            "summary" in prompt.lower()
            or "summarize" in prompt.lower()
            or "summarise" in prompt.lower()
        )


class TestWorkerLoopWiresParaphraser:
    def test_worker_loop_invokes_paraphraser_on_untrusted(self):
        body = WORKER_LOOP.read_text(encoding="utf-8")
        assert "run_paraphraser" in body
        # Untrusted-message path must feed the paraphraser.
        assert "untrusted" in body


class TestMessengerSkillInjectionIsStyleOnly:
    def test_skill_sections_labelled_as_output_style(self):
        src = (
            Path(text_roles_mod.__file__).read_text(encoding="utf-8")
        )
        # The injection header must not claim skills are authoritative
        # planner/worker instructions inside the messenger context —
        # messenger is text production only.
        assert "## Skills (output style)" in src
        assert "## Skills (planner guidance)" not in src
