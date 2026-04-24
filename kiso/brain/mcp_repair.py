"""One-shot LLM-mediated repair for invalid MCP call arguments.

The preflight validator (``kiso.mcp.validate.validate_mcp_args``)
runs before every MCP call. When it fails, rather than replanning
we give the planner one cheap retry: a small LLM call with the
task's natural-language intent, the method's JSON schema, and the
failing args. The LLM returns a revised args object. If the
revision still fails validation, we fall through to the standard
replan path.

The role prompt lives at ``kiso/roles/mcp_repair.md``. The role
reuses the ``worker`` model by default (no separate ``[models]``
entry needed) so existing configs work unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiso.brain.common import _load_system_prompt
from kiso.config import Config
from kiso.llm import call_llm

log = logging.getLogger(__name__)


_ROLE = "mcp_repair"


async def repair_mcp_args(
    *,
    config: Config,
    detail: str,
    schema: Any,
    failing_args: dict,
) -> dict | None:
    """Return a revised args dict, or ``None`` if the repair failed.

    Failure modes (all return ``None``):

    - LLM returned invalid JSON.
    - LLM returned a non-object (list, number, bool, etc.).
    - LLM errored in transport.

    The caller re-validates the returned dict against the schema
    and only dispatches if it passes.
    """
    system_prompt = _load_system_prompt(_ROLE)
    user_content = (
        "# Task intent\n"
        f"{detail}\n\n"
        "# Input schema (JSON Schema)\n"
        f"```json\n{json.dumps(schema, sort_keys=True)}\n```\n\n"
        "# Args that failed validation\n"
        f"```json\n{json.dumps(failing_args, sort_keys=True)}\n```\n\n"
        "# Output\n"
        "Return ONE JSON object that satisfies the schema. No prose."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        raw = await call_llm(
            config,
            role=_ROLE,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("mcp_repair LLM call failed: %s", exc)
        return None

    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
