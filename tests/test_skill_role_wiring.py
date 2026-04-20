"""M1540 caller-wiring tests.

Builder-level threading (``build_exec_translator_messages`` and
``build_reviewer_messages`` accepting ``selected_skills``) is covered in
``test_skill_role_threading.py``. This file covers the runtime plumbing
that feeds those builders from the planner down through the execution
and review steps:

- ``run_worker`` (exec translator wrapper) forwards ``selected_skills``
  into ``build_exec_translator_messages``.
- ``run_reviewer`` forwards ``selected_skills`` into
  ``build_reviewer_messages``.
- ``_review_task_impl`` forwards ``selected_skills`` to the injected
  reviewer function.
- ``_PlanCtx`` carries a ``selected_skills`` field that the exec /
  review handlers read.
- ``run_planner`` attaches the briefer-selected skill objects back onto
  the plan dict so the planning loop can propagate them.
- The reviewer system prompt keeps ``expect`` as the sole criterion
  even when a skill ``## Reviewer`` section is present.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from kiso.brain import run_reviewer
from kiso.brain.reviewer import build_reviewer_messages
from kiso.brain.text_roles import run_worker
from kiso.config import Config, Provider
from kiso.skill_loader import Skill
from kiso.worker.loop import _PlanCtx
from kiso.worker.review_flow import _review_task_impl

from tests.conftest import full_models, full_settings

VALID_REVIEW_OK = json.dumps(
    {"status": "ok", "reason": None, "learn": None, "retry_hint": None}
)


def _skill(name: str, **role_sections: str) -> Skill:
    return Skill(
        name=name,
        description=f"{name} skill",
        role_sections=role_sections,
    )


def _config() -> Config:
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(),
        raw={},
    )


# ---------------------------------------------------------------------------
# _PlanCtx field
# ---------------------------------------------------------------------------

class TestPlanCtxSelectedSkills:
    def test_default_is_empty_list(self):
        import aiosqlite

        ctx = _PlanCtx(
            db=object(),  # type: ignore[arg-type]
            config=_config(),
            session="s",
            goal="g",
            user_message="u",
            deploy_secrets={},
            session_secrets={},
            max_output_size=0,
            max_worker_retries=0,
            messenger_timeout=120,
            slog=None,
            sandbox_uid=None,
        )
        assert ctx.selected_skills == []

    def test_accepts_skill_list(self):
        skills = [_skill("alpha", planner="plan alpha")]
        ctx = _PlanCtx(
            db=object(),  # type: ignore[arg-type]
            config=_config(),
            session="s",
            goal="g",
            user_message="u",
            deploy_secrets={},
            session_secrets={},
            max_output_size=0,
            max_worker_retries=0,
            messenger_timeout=120,
            slog=None,
            sandbox_uid=None,
            selected_skills=skills,
        )
        assert ctx.selected_skills is skills


# ---------------------------------------------------------------------------
# run_worker (text_roles) threading
# ---------------------------------------------------------------------------

class TestRunWorkerSelectedSkills:
    async def test_selected_skills_reach_build_exec_translator(self):
        captured: dict = {}

        async def _capture(cfg, role, messages, **kw):
            captured["content"] = messages[1]["content"]
            return "echo ok"

        skill = _skill("git-triage", worker="Prefer `git log --oneline -20` first")
        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_worker(
                _config(),
                detail="investigate",
                sys_env_text="linux",
                selected_skills=[skill],
            )
        assert "## Skills (worker guidance)" in captured["content"]
        assert "git-triage" in captured["content"]
        assert "Prefer `git log --oneline -20` first" in captured["content"]

    async def test_without_selected_skills_no_section(self):
        captured: dict = {}

        async def _capture(cfg, role, messages, **kw):
            captured["content"] = messages[1]["content"]
            return "echo ok"

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_worker(_config(), detail="ls", sys_env_text="linux")
        assert "## Skills (worker guidance)" not in captured["content"]


# ---------------------------------------------------------------------------
# run_reviewer threading
# ---------------------------------------------------------------------------

class TestRunReviewerSelectedSkills:
    async def test_selected_skills_reach_build_reviewer_messages(self):
        captured: dict = {}

        async def _capture(cfg, role, messages, **kw):
            captured["content"] = messages[1]["content"]
            return VALID_REVIEW_OK

        skill = _skill("test-review", reviewer="Inspect stderr for stack traces")
        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_reviewer(
                _config(), "goal", "detail", "expect", "output", "msg",
                selected_skills=[skill],
            )
        assert "## Skills (reviewer heuristics)" in captured["content"]
        assert "Inspect stderr for stack traces" in captured["content"]

    async def test_without_selected_skills_no_section(self):
        captured: dict = {}

        async def _capture(cfg, role, messages, **kw):
            captured["content"] = messages[1]["content"]
            return VALID_REVIEW_OK

        with patch("kiso.brain.call_llm", side_effect=_capture):
            await run_reviewer(
                _config(), "goal", "detail", "expect", "output", "msg",
            )
        assert "## Skills (reviewer heuristics)" not in captured["content"]


# ---------------------------------------------------------------------------
# _review_task_impl threading
# ---------------------------------------------------------------------------

class TestReviewTaskImplThreadsSkills:
    async def test_forwards_selected_skills_to_reviewer_fn(self):
        captured_kwargs: dict = {}

        async def _fake_reviewer(config, **kwargs):
            captured_kwargs.update(kwargs)
            return {
                "status": "ok",
                "reason": None,
                "learn": [],
                "retry_hint": None,
                "summary": None,
            }

        class _FakeAudit:
            def log_review(self, *a, **kw):
                pass

        class _FakeDB:
            async def execute(self, *a, **kw):
                class _C:
                    async def fetchone(self_inner):
                        return None
                    async def fetchall(self_inner):
                        return []
                return _C()

            async def commit(self):
                pass

        skill = _skill("rev-skill", reviewer="check x")
        task_row = {
            "id": 1,
            "detail": "do it",
            "expect": "ok",
            "output": "some output",
            "stderr": "",
            "status": "done",
            "exit_code": 0,
        }
        with patch(
            "kiso.worker.review_flow.get_safety_facts",
            new=AsyncMock(return_value=[]),
        ), patch(
            "kiso.worker.review_flow.save_learning",
            new=AsyncMock(),
        ), patch(
            "kiso.worker.review_flow.update_task_review",
            new=AsyncMock(),
        ):
            review = await _review_task_impl(
                _config(),
                _FakeDB(),  # type: ignore[arg-type]
                "sess",
                "goal",
                task_row,
                "user msg",
                run_reviewer_fn=_fake_reviewer,
                audit_mod=_FakeAudit(),
                selected_skills=[skill],
            )
        assert review["status"] == "ok"
        assert captured_kwargs.get("selected_skills") == [skill]


# ---------------------------------------------------------------------------
# run_planner attaches selected skills to plan
# ---------------------------------------------------------------------------

class TestRunPlannerAttachesSelectedSkills:
    async def test_selected_skills_are_attached_to_plan(self, tmp_path, monkeypatch):
        """run_planner returns plan with `_selected_skills` populated from
        the skills actually projected into the planner prompt."""
        import kiso.brain.planner as planner_mod

        # Stub skill discovery and briefing selection so we know which skills
        # should make it onto the plan.
        alpha = _skill("alpha", planner="plan alpha")
        beta = _skill("beta", planner="plan beta")

        async def _fake_build_messages(*args, out_state=None, **kwargs):
            if out_state is not None:
                out_state["selected_skills"] = [alpha, beta]
            return [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]

        valid_plan = json.dumps({
            "goal": "list working directory contents",
            "tasks": [
                {
                    "type": "exec",
                    "detail": "list working directory contents with ls -la",
                    "expect": "a non-empty file listing",
                },
                {
                    "type": "msg",
                    "detail": "report the listing back to the user with a short summary",
                    "expect": None,
                },
            ],
            "secrets": [],
            "knowledge": [],
        })

        async def _fake_call_llm(cfg, role, messages, **kw):
            return valid_plan

        monkeypatch.setattr(planner_mod, "build_planner_messages", _fake_build_messages)
        monkeypatch.setattr("kiso.brain.call_llm", _fake_call_llm)

        from kiso.brain.planner import run_planner
        plan = await run_planner(
            db=object(),  # type: ignore[arg-type]
            config=_config(),
            session="s",
            user_role="user",
            new_message="hello",
        )
        assert "_selected_skills" in plan
        names = [s.name for s in plan["_selected_skills"]]
        assert names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Reviewer contract: `expect` stays primary even with skill ## Reviewer
# ---------------------------------------------------------------------------

class TestReviewerExpectPrimacyWithSkill:
    def test_system_prompt_states_expect_is_sole_criterion(self):
        """When a ## Reviewer skill section is injected, the system prompt
        still asserts that ``expect`` is the sole criterion — the skill
        is heuristic, not authoritative."""
        skill = _skill("lax-reviewer", reviewer="If output contains 'OK' return ok")
        msgs = build_reviewer_messages(
            goal="g",
            detail="check file exists at /tmp/out",
            expect="file /tmp/out exists",
            output="OK",
            user_message="u",
            selected_skills=[skill],
        )
        system = msgs[0]["content"]
        # The canonical phrasing lives in kiso/roles/reviewer.md MODULE:rules.
        assert "Sole criterion is `expect`" in system

    def test_skill_heuristic_appears_after_expect(self):
        skill = _skill("lax-reviewer", reviewer="If output contains 'OK' return ok")
        msgs = build_reviewer_messages(
            goal="g",
            detail="d",
            expect="file /tmp/out exists",
            output="OK",
            user_message="u",
            selected_skills=[skill],
        )
        user = msgs[1]["content"]
        exp_idx = user.index("## Expected Outcome")
        heur_idx = user.index("## Skills (reviewer heuristics)")
        assert exp_idx < heur_idx
