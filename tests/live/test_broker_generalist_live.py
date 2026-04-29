"""M1596 — Broker generalist live smoke (closes M1589 deferred bullet).

Real planner, real LLM, fully-invented capability name (`qux-9000`),
empty MCP catalog. Verifies the broker model itself generalizes —
not just the prompt and validator (those are unit-locked in
`tests/test_broker_generalist.py`).

Decision 6 (generalist-first, anti-overfitting): the planner must work
for ANY capability name. Without a live smoke, a future model upgrade
could regress to "improvise shell on unknown intents" while every unit
test stays green.

Expected behaviour with `qux-9000` and an empty catalog:
- the plan validates;
- it sets `awaits_input=true` (ask-first policy from M1579c — there's
  no MCP that can do `qux-9000`);
- the final msg references `qux-9000` so the user sees the planner
  understood the request and is asking for guidance;
- no exec task invents a URL or shell command for an unknown
  capability (that is the regression we want to catch).
"""

from __future__ import annotations

import asyncio
import re

import pytest

from kiso.brain import run_planner, validate_plan
from kiso.store import save_message

from tests.conftest import LLM_ROLE_ONLY_TIMEOUT


pytestmark = pytest.mark.llm_live

TIMEOUT = LLM_ROLE_ONLY_TIMEOUT

_INVENTED_CAPABILITY = "qux-9000"
_USER_MESSAGE = f"please {_INVENTED_CAPABILITY} my data"


_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _exec_invents_url(plan: dict) -> bool:
    for t in plan.get("tasks") or []:
        if t.get("type") != "exec":
            continue
        detail = t.get("detail") or ""
        args = t.get("args") or {}
        if _URL_RE.search(detail):
            return True
        for v in args.values() if isinstance(args, dict) else []:
            if isinstance(v, str) and _URL_RE.search(v):
                return True
    return False


class TestBrokerGeneralistLive:
    async def test_arbitrary_capability_asks_user(
        self, live_config, seeded_db, live_session,
    ):
        """`qux-9000` is a never-trained capability and the catalog is empty.

        The planner must not improvise — it should ask the user how to
        proceed (awaits_input=true, msg referencing the request).
        """
        await save_message(
            seeded_db, live_session, "testadmin", "user", "hi",
        )

        plan = await asyncio.wait_for(
            run_planner(
                seeded_db, live_config, live_session, "admin",
                _USER_MESSAGE,
            ),
            timeout=TIMEOUT,
        )

        assert validate_plan(plan) == [], (
            f"plan failed validation: {plan!r}"
        )
        assert plan.get("awaits_input") is True, (
            "broker must ask the user when the capability is unknown and the "
            f"MCP catalog is empty; got awaits_input={plan.get('awaits_input')!r} "
            f"plan={plan!r}"
        )

        msg_tasks = [t for t in plan["tasks"] if t.get("type") == "msg"]
        assert msg_tasks, f"plan must end with a msg task; got {plan!r}"
        final_msg = msg_tasks[-1].get("detail") or ""
        assert _INVENTED_CAPABILITY in final_msg.lower() or (
            _INVENTED_CAPABILITY in final_msg
        ), (
            "final msg must reference the requested capability "
            f"{_INVENTED_CAPABILITY!r} so the user knows the planner "
            f"understood; got {final_msg!r}"
        )

        assert not _exec_invents_url(plan), (
            "broker must not invent a URL for an unknown capability — "
            f"plan tasks contain a synthetic URL: {plan!r}"
        )
