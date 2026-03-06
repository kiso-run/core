"""Auto-repair unhealthy skills by re-running deps.sh on startup."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kiso.config import KISO_DIR
from kiso.skills import check_deps, discover_skills, invalidate_skills_cache

log = logging.getLogger(__name__)

# Limits to prevent startup from hanging
_PER_SKILL_TIMEOUT = 60  # seconds per skill deps.sh
_TOTAL_TIMEOUT = 180  # seconds total for all repairs


async def repair_unhealthy_skills(skills_dir: Path | None = None) -> list[str]:
    """Re-run deps.sh for skills with missing binary deps.

    Returns list of skill names where repair was attempted.
    """
    resolved_dir = skills_dir or (KISO_DIR / "skills")
    skills = discover_skills(resolved_dir)
    unhealthy = [s for s in skills if not s.get("healthy", True)]

    if not unhealthy:
        return []

    repaired: list[str] = []
    total_start = asyncio.get_event_loop().time()

    for skill in unhealthy:
        elapsed = asyncio.get_event_loop().time() - total_start
        if elapsed >= _TOTAL_TIMEOUT:
            log.warning("Skill repair total timeout (%ds) reached, skipping remaining", _TOTAL_TIMEOUT)
            break

        skill_path = Path(skill["path"])
        deps_sh = skill_path / "deps.sh"
        if not deps_sh.exists():
            log.info("Skill '%s' is unhealthy but has no deps.sh — skipping", skill["name"])
            continue

        log.info("Repairing skill '%s' (missing: %s)...", skill["name"], skill.get("missing_deps", []))
        remaining = min(_PER_SKILL_TIMEOUT, _TOTAL_TIMEOUT - elapsed)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(deps_sh),
                cwd=str(skill_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=remaining)
            if proc.returncode == 0:
                log.info("Skill '%s' deps.sh succeeded", skill["name"])
            else:
                log.warning(
                    "Skill '%s' deps.sh failed (exit %d): %s",
                    skill["name"], proc.returncode, stderr.decode(errors="replace")[:500],
                )
        except asyncio.TimeoutError:
            log.warning("Skill '%s' deps.sh timed out after %ds", skill["name"], remaining)
        except OSError as e:
            log.warning("Skill '%s' deps.sh error: %s", skill["name"], e)

        repaired.append(skill["name"])

    if repaired:
        invalidate_skills_cache()

    return repaired
