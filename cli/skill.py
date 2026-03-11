"""Backward compatibility shim — use cli.tool instead."""

from cli.tool import run_tool_command as run_skill_command  # noqa: F401
from cli.tool import TOOLS_DIR as SKILLS_DIR  # noqa: F401
from cli.tool import OFFICIAL_PREFIX  # noqa: F401
from cli.tool import _tool_post_install as _skill_post_install  # noqa: F401
from cli.tool import _tool_list as _skill_list  # noqa: F401
from cli.tool import _tool_search as _skill_search  # noqa: F401
from cli.tool import _tool_install as _skill_install  # noqa: F401
from cli.tool import _tool_update as _skill_update  # noqa: F401
from cli.tool import _tool_remove as _skill_remove  # noqa: F401
from cli.tool import _is_url, _is_repo_not_found, _require_admin  # noqa: F401
from cli.tool import _fetch_registry, _search_entries  # noqa: F401
