"""Abstract base class for MCP clients.

Two concrete implementations live alongside this module:

- ``kiso.mcp.stdio.MCPStdioClient`` — spawns the server as a subprocess,
  communicates via newline-delimited JSON-RPC on stdin/stdout
- ``kiso.mcp.http.MCPStreamableHTTPClient`` — HTTP POST/GET to a remote
  endpoint, optionally with SSE streams for server-pushed messages

The manager (``kiso.mcp.manager.MCPManager``) holds these
polymorphically, so the rest of kiso (planner integration, worker
dispatch, CLI) never needs to know which transport a given server
uses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from kiso.mcp.schemas import (
    MCPCallResult,
    MCPMethod,
    MCPResource,
    MCPResourceContent,
    MCPServerInfo,
)


class MCPClient(ABC):
    """Contract every MCP transport must implement.

    Lifecycle is ``initialize`` (once) → many ``list_methods`` /
    ``call_method`` / ``cancel`` → ``shutdown`` (once). ``is_healthy``
    is a synchronous probe the manager uses to decide whether a client
    needs to be restarted before the next call.
    """

    @abstractmethod
    async def initialize(self) -> MCPServerInfo:
        """Perform the MCP initialize handshake.

        Sends an ``initialize`` request with kiso's client info and
        minimum capabilities, receives the ``InitializeResult``,
        stores the negotiated protocol version and server capabilities,
        then sends the ``notifications/initialized`` notification.

        Returns the captured ``MCPServerInfo``.

        Raises:
            MCPProtocolError: on version mismatch or malformed response
            MCPTransportError: on pipe / HTTP layer failure
        """

    @abstractmethod
    async def list_methods(self) -> list[MCPMethod]:
        """Fetch and return every method the server exposes.

        Implementations handle ``tools/list`` pagination internally —
        callers receive the full list. Results may be cached by the
        manager layer.
        """

    @abstractmethod
    async def call_method(self, name: str, args: dict) -> MCPCallResult:
        """Invoke a method by name with the given arguments.

        ``name`` is the server-local method name (not the
        ``server:method`` qualified form). Implementations serialize
        ``args`` as the JSON-RPC ``arguments`` field of the ``tools/call``
        request, await the response, and normalise the MCP
        ``content[]`` into an ``MCPCallResult``.

        Raises:
            MCPInvocationError: method unknown, args rejected by input
                schema, or server returned isError: true
            MCPTransportError: transport layer failed mid-call
        """

    @abstractmethod
    async def list_resources(self) -> list[MCPResource]:
        """Fetch and return every resource the server exposes.

        Parallel to :meth:`list_methods` but semantically distinct:
        resources are data, not callable functions. Implementations
        handle ``resources/list`` pagination internally. Servers that
        do not declare the ``resources`` capability during initialize
        return ``[]`` (no error).
        """

    @abstractmethod
    async def read_resource(self, uri: str) -> list[MCPResourceContent]:
        """Read the resource identified by ``uri``.

        Issues ``resources/read`` with ``{"uri": uri}`` and returns the
        list of content blocks. Text resources populate ``text``;
        binary resources populate ``blob`` (base64).

        Raises:
            MCPInvocationError: uri unknown, not readable, or server
                returned an error
            MCPTransportError: transport layer failed mid-call
        """

    @abstractmethod
    async def cancel(self, request_id: Any) -> None:
        """Send a ``notifications/cancelled`` for an in-flight request.

        Fire-and-forget. The manager uses this on task timeout or on
        worker shutdown while a call is still pending. Callers must
        not rely on the cancellation being honoured — servers may
        complete anyway.
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """Terminate the transport cleanly.

        For stdio: close stdin, wait for the subprocess to exit,
        SIGTERM then SIGKILL on timeout. For HTTP: issue
        ``DELETE`` with the session-id header.
        """

    @abstractmethod
    def is_healthy(self) -> bool:
        """Return True if the client is ready to handle new calls.

        Synchronous fast check. False signals the manager to restart
        the client before the next call.
        """
