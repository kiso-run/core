"""Backward compatibility shim — use kiso.tool_repair instead."""

from kiso.tool_repair import repair_unhealthy_tools as repair_unhealthy_skills  # noqa: F401
from kiso.tool_repair import _PER_TOOL_TIMEOUT as _PER_SKILL_TIMEOUT  # noqa: F401
from kiso.tool_repair import _TOTAL_TIMEOUT  # noqa: F401
