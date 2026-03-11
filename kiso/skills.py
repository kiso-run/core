"""Backward compatibility shim — use kiso.tools instead."""

# Re-export everything under old names for callers not yet updated.
# This shim will be removed once all imports are migrated (M437-M446).

from kiso.tools import ToolError as SkillError  # noqa: F401
from kiso.tools import auto_correct_tool_args as auto_correct_skill_args  # noqa: F401
from kiso.tools import build_planner_tool_list as build_planner_skill_list  # noqa: F401
from kiso.tools import build_tool_env as build_skill_env  # noqa: F401
from kiso.tools import build_tool_input as build_skill_input  # noqa: F401
from kiso.tools import check_deps  # noqa: F401
from kiso.tools import discover_tools as discover_skills  # noqa: F401
from kiso.tools import invalidate_tools_cache as invalidate_skills_cache  # noqa: F401
from kiso.tools import validate_tool_args as validate_skill_args  # noqa: F401
from kiso.tools import _env_var_name  # noqa: F401
from kiso.tools import _tool_venv_bin as _skill_venv_bin  # noqa: F401
from kiso.tools import _validate_manifest  # noqa: F401
from kiso.tools import _tools_cache as _skills_cache  # noqa: F401
from kiso.tools import _TOOLS_TTL as _SKILLS_TTL  # noqa: F401
from kiso.tools import MAX_ARGS_SIZE, MAX_ARGS_DEPTH  # noqa: F401
from kiso.tools import _check_args_depth, _coerce_value, _ARG_ALIASES  # noqa: F401
from kiso.tools import _ARG_TYPES  # noqa: F401
from kiso.tools import KISO_DIR  # noqa: F401
