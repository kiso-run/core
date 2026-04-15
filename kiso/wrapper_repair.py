"""Auto-repair unhealthy tools by re-running deps.sh on startup."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from kiso._subprocess_utils import communicate_with_timeout
from kiso.config import KISO_DIR
from kiso.wrappers import check_deps, discover_wrappers, invalidate_wrappers_cache


def _clean_env() -> dict[str, str]:
    """Environment without VIRTUAL_ENV — prevents uv confusion in wrapper venvs."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env

log = logging.getLogger(__name__)

# Image ID file baked into the Docker image at build time.
_IMAGE_ID_PATH = Path("/opt/kiso/.image_id")
# Persisted marker on host volume — survives container rebuilds.
_LAST_IMAGE_ID_PATH = KISO_DIR / ".last_image_id"

# Limits to prevent startup from hanging
_PER_TOOL_TIMEOUT = 60  # seconds per wrapper deps.sh
_TOTAL_TIMEOUT = 180  # seconds total for all repairs


async def repair_unhealthy_wrappers(wrappers_dir: Path | None = None) -> list[str]:
    """Re-run deps.sh for tools with missing binary deps.

    Returns list of wrapper names where repair was attempted.
    """
    resolved_dir = wrappers_dir or (KISO_DIR / "wrappers")
    tools = discover_wrappers(resolved_dir)
    unhealthy = [t for t in tools if not t.get("healthy", True)]

    if not unhealthy:
        return []

    repaired: list[str] = []
    total_start = asyncio.get_event_loop().time()

    for wrapper in unhealthy:
        elapsed = asyncio.get_event_loop().time() - total_start
        if elapsed >= _TOTAL_TIMEOUT:
            log.warning("Wrapper repair total timeout (%ds) reached, skipping remaining", _TOTAL_TIMEOUT)
            break

        wrapper_path = Path(wrapper["path"])
        deps_sh = wrapper_path / "deps.sh"
        if not deps_sh.exists():
            log.info("Wrapper '%s' is unhealthy but has no deps.sh — skipping", wrapper["name"])
            continue

        log.info("Repairing wrapper '%s' (missing: %s)...", wrapper["name"], wrapper.get("missing_deps", []))
        remaining = min(_PER_TOOL_TIMEOUT, _TOTAL_TIMEOUT - elapsed)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(deps_sh),
                cwd=str(wrapper_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_env(),
                # own process group so the helper can
                # killpg the whole tree on timeout.
                start_new_session=True,
            )
            stdout, stderr = await communicate_with_timeout(proc, None, remaining)
            if proc.returncode == 0:
                log.info("Wrapper '%s' deps.sh succeeded", wrapper["name"])
            else:
                log.warning(
                    "Wrapper '%s' deps.sh failed (exit %d): %s",
                    wrapper["name"], proc.returncode, stderr.decode(errors="replace")[:500],
                )
        except asyncio.TimeoutError:
            log.warning("Wrapper '%s' deps.sh timed out after %ds", wrapper["name"], remaining)
        except OSError as e:
            log.warning("Wrapper '%s' deps.sh error: %s", wrapper["name"], e)

        repaired.append(wrapper["name"])

    if repaired:
        invalidate_wrappers_cache()

    return repaired


def _is_container_rebuilt() -> bool:
    """Check if the container image changed since last boot.

    Compares the image ID baked into the Docker image with the last known
    image ID persisted on the host volume. Returns True on first boot or
    after a rebuild.
    """
    if not _IMAGE_ID_PATH.is_file():
        return False  # not running in Docker, or old image without marker
    current = _IMAGE_ID_PATH.read_text().strip()
    if not current:
        return False
    if not _LAST_IMAGE_ID_PATH.is_file():
        return True  # first boot
    last = _LAST_IMAGE_ID_PATH.read_text().strip()
    return current != last


def _mark_image_id() -> None:
    """Persist the current image ID so next boot can detect a rebuild."""
    if not _IMAGE_ID_PATH.is_file():
        return
    current = _IMAGE_ID_PATH.read_text().strip()
    if current:
        _LAST_IMAGE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_IMAGE_ID_PATH.write_text(current)


async def rerun_all_deps(wrappers_dir: Path | None = None) -> list[str]:
    """Re-run deps.sh for ALL installed wrappers (after container rebuild).

    Returns list of wrapper names where deps.sh was executed.
    """
    resolved_dir = wrappers_dir or (KISO_DIR / "wrappers")
    wrappers = discover_wrappers(resolved_dir)
    if not wrappers:
        return []

    log.info("Container rebuilt — re-running deps.sh for %d installed wrapper(s)", len(wrappers))
    executed: list[str] = []
    total_start = asyncio.get_event_loop().time()

    for wrapper in wrappers:
        elapsed = asyncio.get_event_loop().time() - total_start
        if elapsed >= _TOTAL_TIMEOUT:
            log.warning("deps.sh re-run total timeout (%ds) reached, skipping remaining", _TOTAL_TIMEOUT)
            break

        wrapper_path = Path(wrapper["path"])
        deps_sh = wrapper_path / "deps.sh"
        if not deps_sh.exists():
            continue

        log.info("Re-running deps.sh for '%s'...", wrapper["name"])
        remaining = min(_PER_TOOL_TIMEOUT, _TOTAL_TIMEOUT - elapsed)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(deps_sh),
                cwd=str(wrapper_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_env(),
                # own process group so the helper can
                # killpg the whole tree on timeout.
                start_new_session=True,
            )
            stdout, stderr = await communicate_with_timeout(proc, None, remaining)
            if proc.returncode == 0:
                log.info("Wrapper '%s' deps.sh succeeded", wrapper["name"])
            else:
                log.warning(
                    "Wrapper '%s' deps.sh failed (exit %d): %s",
                    wrapper["name"], proc.returncode, stderr.decode(errors="replace")[:500],
                )
        except asyncio.TimeoutError:
            log.warning("Wrapper '%s' deps.sh timed out after %ds", wrapper["name"], remaining)
        except OSError as e:
            log.warning("Wrapper '%s' deps.sh error: %s", wrapper["name"], e)

        executed.append(wrapper["name"])

    if executed:
        invalidate_wrappers_cache()

    return executed
