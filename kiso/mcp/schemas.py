"""Data types and error hierarchy for the MCP client.

All dataclasses here are frozen: instances are immutable once created,
consistent with the rest of kiso's config/schema surface. The error
hierarchy distinguishes four orthogonal failure modes so callers can
react appropriately:

- ``MCPProtocolError`` — peer violated the JSON-RPC / MCP spec
- ``MCPTransportError`` — pipe / HTTP / subprocess layer failed
- ``MCPInvocationError`` — valid protocol but the specific call failed
  (unknown method, invalid args, server-side error)
- ``MCPCapError`` — the caller requested a capability the peer did not
  negotiate

Protocol-internal terminology ("tool") appears only where the spec
mandates it (JSON-RPC method names like ``tools/list``). All
Kiso-facing naming uses "method" — see MCPMethod.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class MCPError(Exception):
    """Base class for all MCP client errors."""


class MCPProtocolError(MCPError):
    """Peer violated the MCP / JSON-RPC protocol (bad handshake, malformed
    response, unexpected message type, etc.)."""


class MCPTransportError(MCPError):
    """Transport layer failed: subprocess died, stdin closed, HTTP 5xx,
    DNS failure, etc. Typically recoverable via restart."""


class MCPInvocationError(MCPError):
    """A specific ``tools/call`` failed with a structured error: unknown
    method, invalid arguments rejected by input schema, or a server-side
    error surfaced via ``isError: true`` in the tool result."""


class MCPCapError(MCPError):
    """The caller requested a capability that the peer did not declare
    during the initialize handshake."""


@dataclass(frozen=True)
class MCPMethod:
    """A single callable method exposed by an MCP server.

    MCP spec terminology calls this a "tool" (cf. ``tools/list``,
    ``tools/call``). Kiso calls it a "method" everywhere it is exposed
    to users or developers, because "tool" was already taken by the
    wrapper subsystem before the rename.

    Fields mirror the MCP spec tool shape:

    - ``server``: the name of the MCP server this method belongs to,
      scoped by the ``[mcp.<server>]`` config section
    - ``name``: server-unique method identifier (e.g. ``create_issue``)
    - ``title``: optional human-readable display name
    - ``description``: human-readable description of what the method
      does
    - ``input_schema``: JSON Schema describing the expected arguments
    - ``output_schema``: optional JSON Schema for structured output
      validation
    - ``annotations``: optional untrusted hints about behavior (e.g.
      ``readOnlyHint``, ``destructiveHint``)

    The ``qualified`` property returns ``"server:name"`` — the canonical
    form used in log lines, briefer output, and user-facing displays.
    """

    server: str
    name: str
    title: str | None
    description: str
    input_schema: dict
    output_schema: dict | None
    annotations: dict | None

    @property
    def qualified(self) -> str:
        return f"{self.server}:{self.name}"


@dataclass(frozen=True)
class MCPResource:
    """A static or dynamic file-like object exposed by an MCP server.

    Resources are the MCP spec's second primitive (alongside tools and
    prompts). Semantically they are data, not functions: log files,
    database rows, documentation pages, generated reports. Kiso lists
    them, surfaces them to the planner, and reads them on demand via
    the synthetic ``__resource_read`` method routed through the worker
    MCP task handler.

    Fields mirror the MCP spec resource shape:

    - ``server``: the name of the MCP server this resource belongs to
    - ``uri``: canonical resource identifier (e.g. ``file:///logs/today.log``)
    - ``name``: server-local short name (may collide across servers)
    - ``description``: human-readable description
    - ``mime_type``: optional MIME hint (``text/plain``, ``image/png``)

    The ``qualified`` property returns ``"server:uri"`` — the canonical
    form used in log lines, briefer output, and user-facing displays.
    """

    server: str
    uri: str
    name: str
    description: str
    mime_type: str | None

    @property
    def qualified(self) -> str:
        return f"{self.server}:{self.uri}"


@dataclass(frozen=True)
class MCPResourceContent:
    """A single block returned by ``resources/read``.

    MCP's ``resources/read`` response is a list of content blocks —
    one resource can expand into multiple bodies (e.g. a paginated
    report). Each block carries either ``text`` or a base64-encoded
    ``blob``, plus optional MIME type and URI hints.
    """

    uri: str
    mime_type: str | None
    text: str | None
    blob: str | None


@dataclass(frozen=True)
class MCPServerInfo:
    """Server identity and capabilities negotiated during initialize.

    Captured from the ``InitializeResult`` response and held by the
    client for the lifetime of the connection.
    """

    name: str
    title: str | None
    version: str
    protocol_version: str
    capabilities: dict
    instructions: str | None


@dataclass(frozen=True)
class MCPCallResult:
    """Kiso-internal rendering of an MCP ``tools/call`` result.

    The raw MCP ``content[]`` field with its heterogeneous content types
    (text, image, audio, resource_link, embedded_resource,
    structuredContent) is normalised here into a shape the worker
    dispatch can consume directly:

    - ``stdout_text``: concatenated text + structured content (the
      reviewer and messenger read this as if it were a wrapper stdout)
    - ``published_files``: list of (relative_name, absolute_path) pairs
      for binary content saved into the session workspace, to be
      surfaced via the standard ``Published files:`` marker pattern
    - ``structured_content``: original structured content (if any) for
      consumers that want typed access rather than parsing the
      stringified form
    - ``is_error``: True when the MCP server returned
      ``isError: true`` on the content, mapping to a failed task in
      the worker dispatch
    """

    stdout_text: str
    published_files: list
    structured_content: dict | None
    is_error: bool
