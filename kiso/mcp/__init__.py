"""MCP client package: consumer-only client for the Model Context Protocol.

Kiso connects to external MCP servers (stdio or Streamable HTTP) brought
by the user from the wider ecosystem. Kiso does NOT publish MCP servers
and does NOT maintain a curated registry.

Public exports (imported by the rest of kiso and the tests):

- Data types: ``MCPMethod``, ``MCPServerInfo``, ``MCPCallResult``,
  ``MCPServer``
- Error hierarchy: ``MCPError`` (base), ``MCPProtocolError``,
  ``MCPTransportError``, ``MCPInvocationError``, ``MCPCapError``,
  ``MCPConfigError``
- Client ABC: ``MCPClient``
- Config parser: ``parse_mcp_section``
"""

from __future__ import annotations

from kiso.mcp.client import MCPClient
from kiso.mcp.config import MCPConfigError, MCPServer, parse_mcp_section
from kiso.mcp.http import MCPStreamableHTTPClient
from kiso.mcp.schemas import (
    MCPCallResult,
    MCPCapError,
    MCPError,
    MCPInvocationError,
    MCPMethod,
    MCPProtocolError,
    MCPServerInfo,
    MCPTransportError,
)
from kiso.mcp.stdio import MCPStdioClient

__all__ = [
    "MCPCallResult",
    "MCPCapError",
    "MCPClient",
    "MCPConfigError",
    "MCPError",
    "MCPInvocationError",
    "MCPMethod",
    "MCPProtocolError",
    "MCPServer",
    "MCPServerInfo",
    "MCPStdioClient",
    "MCPStreamableHTTPClient",
    "MCPTransportError",
    "parse_mcp_section",
]
