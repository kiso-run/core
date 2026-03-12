"""Auto-repair unhealthy tools by re-running deps.sh on startup."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.tools import check_deps, discover_tools, invalidate_tools_cache

log = logging.getLogger(__name__)

# Limits to prevent startup from hanging
_PER_TOOL_TIMEOUT = 60  # seconds per tool deps.sh
_TOTAL_TIMEOUT = 180  # seconds total for all repairs


async def repair_unhealthy_tools(tools_dir: Path | None = None) -> list[str]:
    """Re-run deps.sh for tools with missing binary deps.

    Returns list of tool names where repair was attempted.
    """
    resolved_dir = tools_dir or (KISO_DIR / "tools")
    tools = discover_tools(resolved_dir)
    unhealthy = [t for t in tools if not t.get("healthy", True)]

    if not unhealthy:
        return []

    repaired: list[str] = []
    total_start = asyncio.get_event_loop().time()

    for tool in unhealthy:
        elapsed = asyncio.get_event_loop().time() - total_start
        if elapsed >= _TOTAL_TIMEOUT:
            log.warning("Tool repair total timeout (%ds) reached, skipping remaining", _TOTAL_TIMEOUT)
            break

        tool_path = Path(tool["path"])
        deps_sh = tool_path / "deps.sh"
        if not deps_sh.exists():
            log.info("Tool '%s' is unhealthy but has no deps.sh — skipping", tool["name"])
            continue

        log.info("Repairing tool '%s' (missing: %s)...", tool["name"], tool.get("missing_deps", []))
        remaining = min(_PER_TOOL_TIMEOUT, _TOTAL_TIMEOUT - elapsed)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(deps_sh),
                cwd=str(tool_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=remaining)
            if proc.returncode == 0:
                log.info("Tool '%s' deps.sh succeeded", tool["name"])
            else:
                log.warning(
                    "Tool '%s' deps.sh failed (exit %d): %s",
                    tool["name"], proc.returncode, stderr.decode(errors="replace")[:500],
                )
        except asyncio.TimeoutError:
            log.warning("Tool '%s' deps.sh timed out after %ds", tool["name"], remaining)
        except OSError as e:
            log.warning("Tool '%s' deps.sh error: %s", tool["name"], e)

        repaired.append(tool["name"])

    if repaired:
        invalidate_tools_cache()

    return repaired
