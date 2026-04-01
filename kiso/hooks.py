"""Pre/post execution hooks for extensibility.

Hooks are shell commands defined in config.toml that run before/after
exec task subprocess execution. Pre-exec hooks can block execution
(non-zero exit = task fails). Post-exec hooks are fire-and-forget.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_HOOK_TIMEOUT = 10  # seconds


@dataclass(frozen=True, slots=True)
class HookResult:
    """Result of running a hook."""
    allowed: bool
    message: str  # stderr from blocking hook on denial, empty otherwise


async def run_pre_exec_hooks(
    hooks: list[dict],
    command: str,
    detail: str,
    session: str,
    task_id: int,
) -> HookResult:
    """Run pre-exec hooks. Returns HookResult; allowed=False blocks execution."""
    for hook in hooks:
        cmd = hook.get("command", "")
        if not cmd:
            continue
        blocking = hook.get("blocking", False)
        context = json.dumps({
            "event": "pre_exec",
            "command": command,
            "detail": detail,
            "session": session,
            "task_id": task_id,
        })
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(context.encode()),
                timeout=_HOOK_TIMEOUT,
            )
            if blocking and proc.returncode != 0:
                msg = stderr.decode(errors="replace").strip() or "blocked by pre-exec hook"
                log.warning("Pre-exec hook blocked task %d: %s", task_id, msg)
                return HookResult(allowed=False, message=msg)
        except asyncio.TimeoutError:
            log.warning("Pre-exec hook timed out after %ds, allowing execution", _HOOK_TIMEOUT)
        except OSError as e:
            log.warning("Pre-exec hook failed to run: %s", e)
    return HookResult(allowed=True, message="")


async def run_post_exec_hooks(
    hooks: list[dict],
    command: str,
    detail: str,
    session: str,
    task_id: int,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> None:
    """Run post-exec hooks (fire-and-forget, non-blocking)."""
    for hook in hooks:
        cmd = hook.get("command", "")
        if not cmd:
            continue
        context = json.dumps({
            "event": "post_exec",
            "command": command,
            "detail": detail,
            "session": session,
            "task_id": task_id,
            "exit_code": exit_code,
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
        })
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(
                proc.communicate(context.encode()),
                timeout=_HOOK_TIMEOUT,
            )
        except (asyncio.TimeoutError, OSError) as e:
            log.warning("Post-exec hook failed: %s", e)
