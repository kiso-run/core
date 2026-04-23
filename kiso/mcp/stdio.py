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
import stat
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
    MCPPrompt,
    MCPPromptArgument,
    MCPPromptMessage,
    MCPPromptResult,
    MCPProtocolError,
    MCPResource,
    MCPResourceContent,
    MCPServerInfo,
    MCPTransportError,
)

log = logging.getLogger(__name__)

CLIENT_PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "kiso"
CLIENT_VERSION = "0.9.0"

_STDERR_RING_MAX_BYTES = 1 * 1024 * 1024  # 1 MB per server in memory
_SHUTDOWN_GRACE_S = 5.0

_WORLD_READABLE_BITS = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH


def _read_env_file(server_name: str) -> dict[str, str]:
    """Read ``~/.kiso/mcp/<server_name>.env`` if present and mode 0600.

    Returns an empty dict on missing file or insecure mode (world-readable
    files are refused — a stray 0644 creds file never reaches the
    subprocess). ``KISO_*`` keys are dropped with a warning.
    """
    from kiso.mcp.envfile import env_file_path, parse_env_file_text

    path = env_file_path(server_name)
    try:
        st = path.stat()
    except OSError:
        return {}
    if st.st_mode & _WORLD_READABLE_BITS:
        log.warning(
            "mcp[%s] refusing to load %s: mode 0%o is not 0600",
            server_name, path, stat.S_IMODE(st.st_mode),
        )
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("mcp[%s] cannot read %s: %s", server_name, path, e)
        return {}

    out: dict[str, str] = {}
    for key, value in parse_env_file_text(text).items():
        if key.startswith("KISO_"):
            log.warning(
                "mcp[%s] %s sets reserved key %s — dropping",
                server_name, path, key,
            )
            continue
        out[key] = value
    return out


class MCPStdioClient(MCPClient):
    """Concrete MCP client over a local subprocess stdio transport."""

    def __init__(
        self,
        server: MCPServer,
        *,
        extra_env: dict[str, str] | None = None,
        sandbox_uid: int | None = None,
        config: Any | None = None,
    ) -> None:
        if server.transport != "stdio":
            raise ValueError(
                f"MCPStdioClient requires transport='stdio', got {server.transport!r}"
            )
        self._server = server
        self._extra_env = dict(extra_env or {})
        self._sandbox_uid = sandbox_uid
        self._config = config
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
                    "capabilities": self._build_client_capabilities(),
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

    async def list_resources(self) -> list[MCPResource]:
        self._require_initialized()
        caps = (self._server_info.capabilities if self._server_info else {}) or {}
        if "resources" not in caps:
            return []
        resources: list[MCPResource] = []
        cursor: str | None = None
        while True:
            params: dict = {}
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._request(
                "resources/list", params, timeout=self._server.timeout_s
            )
            if "error" in response:
                raise MCPInvocationError(
                    f"resources/list failed: {response['error']}"
                )
            result = response.get("result") or {}
            for raw in result.get("resources") or []:
                resources.append(
                    MCPResource(
                        server=self._server.name,
                        uri=raw.get("uri", ""),
                        name=raw.get("name", ""),
                        description=raw.get("description", ""),
                        mime_type=raw.get("mimeType"),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return resources

    async def read_resource(self, uri: str) -> list[MCPResourceContent]:
        self._require_initialized()
        response = await self._request(
            "resources/read", {"uri": uri}, timeout=self._server.timeout_s
        )
        if "error" in response:
            err = response["error"]
            msg = err.get("message") or str(err)
            raise MCPInvocationError(f"resources/read {uri}: {msg}")
        result = response.get("result") or {}
        return _build_resource_blocks(result)

    async def list_prompts(self) -> list[MCPPrompt]:
        self._require_initialized()
        caps = (self._server_info.capabilities if self._server_info else {}) or {}
        if "prompts" not in caps:
            return []
        prompts: list[MCPPrompt] = []
        cursor: str | None = None
        while True:
            params: dict = {}
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._request(
                "prompts/list", params, timeout=self._server.timeout_s
            )
            if "error" in response:
                raise MCPInvocationError(
                    f"prompts/list failed: {response['error']}"
                )
            result = response.get("result") or {}
            for raw in result.get("prompts") or []:
                prompts.append(_build_prompt(self._server.name, raw))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return prompts

    async def get_prompt(self, name: str, args: dict) -> MCPPromptResult:
        self._require_initialized()
        response = await self._request(
            "prompts/get",
            {"name": name, "arguments": args or {}},
            timeout=self._server.timeout_s,
        )
        if "error" in response:
            err = response["error"]
            msg = err.get("message") or str(err)
            raise MCPInvocationError(f"prompts/get {name}: {msg}")
        result = response.get("result") or {}
        return _build_prompt_result(result)

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

    @property
    def advertises_sampling(self) -> bool:
        """Whether the initialize handshake declared ``sampling`` support."""
        caps = self._build_client_capabilities()
        return "sampling" in caps

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_client_capabilities(self) -> dict:
        caps: dict = {}
        config = self._config
        if config is not None:
            try:
                from kiso.config import setting_bool
                if setting_bool(
                    config.settings, "mcp_sampling_enabled", default=True
                ):
                    caps["sampling"] = {}
            except Exception:  # noqa: BLE001 — defensive
                pass
        return caps

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MCPProtocolError("client not initialized")

    async def _spawn(self) -> None:
        env = self._build_env()
        cwd = self._server.cwd
        spawn_kwargs: dict[str, Any] = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        if self._sandbox_uid is not None:
            # asyncio forwards `user=` to setuid() in the child between
            # fork and exec, dropping privileges before the MCP server
            # sees any input — same behavior exec tasks already get.
            spawn_kwargs["user"] = self._sandbox_uid
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._server.command,
                *self._server.args,
                **spawn_kwargs,
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
        """Assemble the subprocess env.

        Layers (later wins):
        1. ``os.environ`` minus ``KISO_*`` (kiso-internal secrets).
        2. Server ``env`` from the config (already denied ``KISO_*`` at parse).
        3. Session-scoped extra env (``MCPManager.set_session_env``).
        4. ``~/.kiso/mcp/<name>.env`` — the user's persistent credential
           file. It wins so ``kiso mcp env set`` is the authoritative
           override.
        5. ``OAUTH_TOKEN`` when the manager has resolved an OAuth token.
        """
        base = {k: v for k, v in os.environ.items() if not k.startswith("KISO_")}
        base.update(self._server.env)
        base.update(self._extra_env)
        base.update(_read_env_file(self._server.name))
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
        # Responses have an id matching a pending request; server-to-client
        # requests have both ``method`` and ``id`` (we reply by writing a
        # response with the same id); notifications have ``method`` but no
        # id (we ignore them unless we care about a specific one).
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg.get("id")
            fut = self._pending.pop(req_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        method = msg.get("method")
        if "id" in msg and method:
            asyncio.create_task(
                self._handle_incoming_request(msg),
                name=f"mcp-stdio-{self._server.name}-incoming-{msg.get('id')}",
            )
            return
        log.debug("mcp[%s] dispatched non-response: %s", self._server.name, method)

    async def _handle_incoming_request(self, req: dict) -> None:
        """Process a server-to-client JSON-RPC request and write the reply."""
        from kiso.mcp.sampling import SAMPLING_METHOD, handle_sampling_request

        method = req.get("method")
        req_id = req.get("id")
        try:
            if method == SAMPLING_METHOD:
                if self._config is None:
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": (
                                "sampling/createMessage not supported: "
                                "client has no config bound"
                            ),
                        },
                    }
                else:
                    response = await handle_sampling_request(self._config, req)
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"method not found: {method!r}",
                    },
                }
        except Exception as exc:  # noqa: BLE001 — never let the dispatch task die
            log.exception(
                "mcp[%s] incoming request handler crashed", self._server.name,
            )
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"handler crashed: {exc}"},
            }
        await self._write_raw(response)

    async def _write_raw(self, payload: dict) -> None:
        """Serialize *payload* as one JSON-RPC line and drain stdin."""
        if self._proc is None or self._proc.stdin is None:
            return
        line = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "mcp[%s] stdin write failed for %s: %s",
                self._server.name, payload.get("method") or "response", exc,
            )

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


def _build_prompt(server: str, raw: dict) -> MCPPrompt:
    arguments: list[MCPPromptArgument] = []
    for arg in raw.get("arguments") or []:
        if not isinstance(arg, dict):
            continue
        arguments.append(
            MCPPromptArgument(
                name=str(arg.get("name", "")),
                description=str(arg.get("description", "") or ""),
                required=bool(arg.get("required", False)),
            )
        )
    return MCPPrompt(
        server=server,
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "") or ""),
        arguments=arguments,
    )


def _flatten_prompt_content(content: Any) -> str:
    """Flatten an MCP prompt message ``content`` into a single string.

    The spec accepts either a single content block or a list; text
    blocks contribute their ``text``, image/audio blocks degrade to a
    typed placeholder, embedded resources inline their ``text``.
    """
    items: list[dict]
    if isinstance(content, dict):
        items = [content]
    elif isinstance(content, list):
        items = [c for c in content if isinstance(c, dict)]
    else:
        return "" if content is None else str(content)

    lines: list[str] = []
    for item in items:
        itype = item.get("type")
        if itype == "text":
            lines.append(str(item.get("text", "")))
        elif itype in ("image", "audio"):
            mime = item.get("mimeType", f"{itype}/?")
            lines.append(f"[{itype}: {mime}]")
        elif itype == "resource":
            res = item.get("resource") or {}
            uri = res.get("uri", "")
            text = res.get("text")
            if isinstance(text, str):
                lines.append(f"[resource: {uri}]\n{text}")
            else:
                lines.append(f"[resource: {uri}]")
        else:
            lines.append(f"[content type {itype!r}]")
    return "\n".join(lines)


def _build_prompt_result(result: dict) -> MCPPromptResult:
    messages: list[MCPPromptMessage] = []
    for raw in result.get("messages") or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", "user"))
        text = _flatten_prompt_content(raw.get("content"))
        messages.append(MCPPromptMessage(role=role, text=text))
    description = str(result.get("description", "") or "")
    return MCPPromptResult(description=description, messages=messages)


def _build_resource_blocks(result: dict) -> list[MCPResourceContent]:
    """Normalise a ``resources/read`` result into a list of content blocks."""
    blocks: list[MCPResourceContent] = []
    for item in result.get("contents") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text") if isinstance(item.get("text"), str) else None
        blob = item.get("blob") if isinstance(item.get("blob"), str) else None
        blocks.append(
            MCPResourceContent(
                uri=item.get("uri", ""),
                mime_type=item.get("mimeType"),
                text=text,
                blob=blob,
            )
        )
    return blocks


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
