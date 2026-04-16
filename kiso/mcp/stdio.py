"""stdio transport for the MCP client.

Spawns the configured MCP server as a subprocess and speaks
newline-delimited JSON-RPC 2.0 on its stdin/stdout, per the MCP
spec. The stderr pipe is drained continuously by a dedicated reader
task into an in-memory ring buffer and an append-only log file at
``~/.kiso/mcp/<name>.err.log`` — this prevents the well-known
deadlock where a chatty server fills the pipe and blocks the
dispatch loop.

Lifecycle:

1. ``initialize()`` spawns the subprocess, starts the reader tasks,
   sends the ``initialize`` request, awaits the response, sends the
   ``notifications/initialized`` notification, and returns the
   captured ``MCPServerInfo``.
2. Normal operation: ``list_methods``, ``call_method``, ``cancel``
   in any order. Requests are correlated by id; the stdout reader
   dispatches responses to pending futures held in a dict.
3. ``shutdown()`` closes stdin, waits up to ``_shutdown_grace_s``
   for the subprocess to exit on its own, escalates to SIGTERM,
   waits again, escalates to SIGKILL. Idempotent: safe to call
   multiple times, safe to call before ``initialize``.

Protocol vocabulary note: the JSON-RPC method names here
(``initialize``, ``notifications/initialized``, ``tools/list``,
``tools/call``, ``notifications/cancelled``) are fixed by the MCP
specification. They appear only inside this module and the future
HTTP transport module; all Kiso-facing vocabulary uses "method"
(cf. ``MCPMethod``, ``list_methods``, ``call_method``).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any

from kiso.config import KISO_DIR
from kiso.mcp.client import MCPClient
from kiso.mcp.config import MCPServer
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPError,
    MCPInvocationError,
    MCPMethod,
    MCPProtocolError,
    MCPServerInfo,
    MCPTransportError,
)

log = logging.getLogger(__name__)

CLIENT_PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "kiso"
CLIENT_VERSION = "0.9.0"

_STDERR_RING_MAX_BYTES = 1 * 1024 * 1024  # 1 MB per server in memory
_SHUTDOWN_GRACE_S = 5.0


class MCPStdioClient(MCPClient):
    """Concrete MCP client over a local subprocess stdio transport."""

    def __init__(self, server: MCPServer) -> None:
        if server.transport != "stdio":
            raise ValueError(
                f"MCPStdioClient requires transport='stdio', got {server.transport!r}"
            )
        self._server = server
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_reader_task: asyncio.Task | None = None
        self._stderr_reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._initialized = False
        self._server_info: MCPServerInfo | None = None
        self._stderr_ring: collections.deque[bytes] = collections.deque()
        self._stderr_ring_bytes = 0
        self._stderr_log: Any = None  # file handle
        self._shutdown_grace_s = _SHUTDOWN_GRACE_S
        self._shutting_down = False
        self._stdout_closed = False
        self._auth_token: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> MCPServerInfo:
        if self._initialized:
            raise MCPProtocolError("client already initialized")
        if self._proc is not None:
            raise MCPProtocolError("client already has a subprocess")

        # Resolve auth before spawning so the token is available
        # in _build_env (M1374).
        from kiso.mcp.auth import resolve_auth
        self._auth_token = resolve_auth(self._server)

        await self._spawn()

        try:
            response = await self._request(
                "initialize",
                {
                    "protocolVersion": CLIENT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": CLIENT_NAME,
                        "title": "Kiso MCP Client",
                        "version": CLIENT_VERSION,
                    },
                },
                timeout=self._server.timeout_s,
            )
        except MCPError:
            # Initialization failed; make sure the subprocess is cleaned up
            # so the caller does not leak a zombie.
            await self.shutdown()
            raise
        except Exception as e:  # noqa: BLE001
            await self.shutdown()
            raise MCPTransportError(f"initialize failed: {e}") from e

        if "result" not in response:
            raise MCPProtocolError(
                f"initialize: no result field in response: {response!r}"
            )
        result = response["result"]
        server_info = result.get("serverInfo") or {}
        info = MCPServerInfo(
            name=server_info.get("name", "unknown"),
            title=server_info.get("title"),
            version=server_info.get("version", "0.0.0"),
            protocol_version=result.get("protocolVersion", ""),
            capabilities=result.get("capabilities") or {},
            instructions=result.get("instructions"),
        )
        self._server_info = info

        await self._notify("notifications/initialized", {})
        self._initialized = True
        return info

    async def list_methods(self) -> list[MCPMethod]:
        self._require_initialized()
        methods: list[MCPMethod] = []
        cursor: str | None = None
        while True:
            params: dict = {}
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._request(
                "tools/list", params, timeout=self._server.timeout_s
            )
            if "error" in response:
                raise MCPInvocationError(
                    f"tools/list failed: {response['error']}"
                )
            result = response.get("result") or {}
            for raw in result.get("tools") or []:
                methods.append(
                    MCPMethod(
                        server=self._server.name,
                        name=raw.get("name", ""),
                        title=raw.get("title"),
                        description=raw.get("description", ""),
                        input_schema=raw.get("inputSchema") or {},
                        output_schema=raw.get("outputSchema"),
                        annotations=raw.get("annotations"),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return methods

    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        self._require_initialized()
        try:
            response = await self._request(
                "tools/call",
                {"name": name, "arguments": args or {}},
                timeout=self._server.timeout_s,
            )
        except MCPTransportError:
            raise
        if "error" in response:
            err = response["error"]
            msg = err.get("message") or str(err)
            raise MCPInvocationError(f"tools/call {name}: {msg}")
        result = response.get("result") or {}
        return _build_call_result(result)

    async def cancel(self, request_id: Any) -> None:
        if self._proc is None:
            return
        try:
            await self._notify(
                "notifications/cancelled",
                {"requestId": request_id, "reason": "client cancelled"},
            )
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] cancel notification failed: %s", self._server.name, e)

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True

        proc = self._proc
        if proc is None:
            self._close_stderr_log()
            return

        # Reject any pending requests so their awaiters can exit.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    MCPTransportError("client shutting down, request aborted")
                )
        self._pending.clear()

        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception as e:  # noqa: BLE001
                    log.debug("mcp[%s] stdin close failed: %s", self._server.name, e)

            # Grace window: let the server exit on its own.
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._shutdown_grace_s)
            except asyncio.TimeoutError:
                await self._escalate_sigterm(proc)
        finally:
            # Drain reader tasks
            for task in (self._stdout_reader_task, self._stderr_reader_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._stdout_reader_task = None
            self._stderr_reader_task = None

            self._close_stderr_log()
            self._initialized = False

    def is_healthy(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.returncode is not None:
            return False
        if self._stdout_closed:
            return False
        return self._initialized

    def stderr_tail(self, max_bytes: int = 4096) -> bytes:
        """Return the last *max_bytes* of captured stderr."""
        data = b"".join(self._stderr_ring)
        return data[-max_bytes:]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MCPProtocolError("client not initialized")

    async def _spawn(self) -> None:
        env = self._build_env()
        cwd = self._server.cwd
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._server.command,
                *self._server.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            raise MCPTransportError(
                f"mcp[{self._server.name}] spawn failed: command not found: "
                f"{self._server.command!r}"
            ) from e
        except Exception as e:  # noqa: BLE001
            raise MCPTransportError(
                f"mcp[{self._server.name}] spawn failed: {e}"
            ) from e

        self._open_stderr_log()
        self._stdout_reader_task = asyncio.create_task(
            self._read_stdout_loop(), name=f"mcp-stdio-{self._server.name}-stdout"
        )
        self._stderr_reader_task = asyncio.create_task(
            self._read_stderr_loop(), name=f"mcp-stdio-{self._server.name}-stderr"
        )

    def _build_env(self) -> dict[str, str]:
        """Inherit os.environ minus KISO_* secrets, overlay server env."""
        base = {k: v for k, v in os.environ.items() if not k.startswith("KISO_")}
        # Preserve explicit non-secret KISO_ vars if any exist (none today).
        # Overlay server-specific env (already denied KISO_* at parse time).
        base.update(self._server.env)
        # Inject resolved OAuth token (M1374) so the MCP server can
        # read it from env. Convention: OAUTH_TOKEN is not standardized
        # by the MCP spec but is the most common pattern for stdio
        # servers that need auth.
        if self._auth_token:
            base["OAUTH_TOKEN"] = self._auth_token
        return base

    def _open_stderr_log(self) -> None:
        try:
            log_dir = KISO_DIR / "mcp"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._stderr_log = open(
                log_dir / f"{self._server.name}.err.log", "ab", buffering=0
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "mcp[%s] cannot open stderr log: %s — stderr will ring-buffer only",
                self._server.name, e,
            )
            self._stderr_log = None

    def _close_stderr_log(self) -> None:
        if self._stderr_log is not None:
            try:
                self._stderr_log.close()
            except Exception:  # noqa: BLE001
                pass
            self._stderr_log = None

    async def _read_stdout_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            try:
                line = await stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._abort_pending(MCPTransportError(f"stdout read failed: {e}"))
                return
            if not line:
                # EOF — process exited or closed stdout. Mark the channel
                # dead so is_healthy flips to False even if the child has
                # not been reaped yet by the event loop.
                self._stdout_closed = True
                self._abort_pending(
                    MCPTransportError(
                        f"mcp[{self._server.name}] subprocess closed stdout "
                        f"with {len(self._pending)} pending request(s)"
                    )
                )
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                # Non-JSON line on stdout: per MCP spec, server MUST NOT write
                # non-MCP to stdout, but we recover gracefully by logging
                # and skipping.
                log.debug(
                    "mcp[%s] discarded non-JSON stdout line: %s",
                    self._server.name, text[:200],
                )
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        # Responses have an id matching a pending request; notifications
        # have a method but no id; requests FROM the server (rare, would
        # require roots/sampling capabilities we don't advertise) also
        # have a method.
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg.get("id")
            fut = self._pending.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        # Any other message type (notification, unexpected request) is
        # ignored for now — we did not advertise capabilities that would
        # let a server initiate requests toward us.
        log.debug("mcp[%s] dispatched non-response: %s", self._server.name, msg.get("method"))

    def _abort_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _read_stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while True:
            try:
                chunk = await stderr.read(4096)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("mcp[%s] stderr read failed: %s", self._server.name, e)
                return
            if not chunk:
                return
            self._stderr_ring.append(chunk)
            self._stderr_ring_bytes += len(chunk)
            while self._stderr_ring_bytes > _STDERR_RING_MAX_BYTES and self._stderr_ring:
                removed = self._stderr_ring.popleft()
                self._stderr_ring_bytes -= len(removed)
            if self._stderr_log is not None:
                try:
                    self._stderr_log.write(chunk)
                except Exception as e:  # noqa: BLE001
                    log.debug("mcp[%s] stderr log write failed: %s", self._server.name, e)

    async def _request(
        self,
        method: str,
        params: dict,
        *,
        timeout: float,
    ) -> dict:
        assert self._proc is not None and self._proc.stdin is not None
        req_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._pending.pop(req_id, None)
            raise MCPTransportError(f"stdin write failed: {e}") from e
        except Exception as e:  # noqa: BLE001
            self._pending.pop(req_id, None)
            raise MCPTransportError(f"stdin write failed: {e}") from e
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(req_id, None)
            # Best-effort cancellation notification
            try:
                await self._notify(
                    "notifications/cancelled",
                    {"requestId": req_id, "reason": "client timeout"},
                )
            except Exception:  # noqa: BLE001
                pass
            raise MCPTransportError(
                f"mcp[{self._server.name}] {method} timed out after {timeout}s"
            ) from e

    async def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # noqa: BLE001
            log.debug("mcp[%s] notify %s failed: %s", self._server.name, method, e)

    async def _escalate_sigterm(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            # Signal the whole process group so children die with the leader.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._shutdown_grace_s)
            return
        except asyncio.TimeoutError:
            pass
        # Last resort
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._shutdown_grace_s)
        except asyncio.TimeoutError:
            log.error(
                "mcp[%s] did not exit even after SIGKILL", self._server.name
            )


def _build_call_result(result: dict) -> MCPCallResult:
    """Minimal rendering of a tools/call result.

    The full content-type mapping (image/audio/resource_link/embedded
    → workspace files with Published files: marker) lands in a later
    milestone alongside the worker dispatch. For now this handles text
    and structured content, and surfaces isError. Binary content types
    are preserved as skeletal text placeholders so callers at least
    know something was returned.
    """
    content = result.get("content") or []
    structured = result.get("structuredContent")
    is_error = bool(result.get("isError"))

    lines: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text":
            lines.append(str(item.get("text", "")))
        elif itype == "image":
            mime = item.get("mimeType", "image/?")
            lines.append(f"[image content: {mime}]")
        elif itype == "audio":
            mime = item.get("mimeType", "audio/?")
            lines.append(f"[audio content: {mime}]")
        elif itype == "resource_link":
            uri = item.get("uri", "")
            lines.append(f"[resource link: {uri}]")
        elif itype == "resource":
            res = item.get("resource") or {}
            uri = res.get("uri", "")
            text = res.get("text")
            if text:
                lines.append(f"[embedded resource {uri}]\n{text}")
            else:
                lines.append(f"[embedded resource: {uri}]")
        else:
            lines.append(f"[content of type {itype!r}]")

    if structured is not None and not lines:
        lines.append(json.dumps(structured))
    elif structured is not None:
        lines.append(json.dumps(structured))

    if is_error:
        text = "\n".join(lines) if lines else "tool returned isError with no content"
        raise MCPInvocationError(text)

    return MCPCallResult(
        stdout_text="\n".join(lines),
        published_files=[],
        structured_content=structured if isinstance(structured, dict) else None,
        is_error=False,
    )
