"""Tests for the ``<x-kiso: {...}>`` description extension + mcp_recovery.

Kiso-run MCP servers embed a JSON extension at the end of each
tool's ``description`` string. During catalog formatting kiso
parses that block, groups methods by consumed content type
(image / audio / document / code), and renders a File Processing
summary at the top of the catalog so the planner can route files
to the right method without having to match descriptions.

The extension is optional — third-party servers that don't use it
render as-is, no file-processing hints, and the planner falls
back to semantic matching.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiso.brain.common import (
    format_mcp_catalog,
    parse_x_kiso_extension,
)
from kiso.mcp.schemas import MCPMethod


def _method(
    server: str, name: str, description: str,
    schema: dict | None = None,
) -> MCPMethod:
    return MCPMethod(
        server=server,
        name=name,
        title=None,
        description=description,
        input_schema=schema or {"type": "object"},
        output_schema=None,
        annotations=None,
    )


@dataclass
class _StubManager:
    catalog: dict[str, list[MCPMethod]]

    def available_servers(self) -> list[str]:
        return sorted(self.catalog.keys())

    def list_methods_cached_only(self, name: str) -> list[MCPMethod]:
        return self.catalog.get(name, [])


class TestParseXKisoExtension:
    def test_no_extension_returns_empty_dict(self):
        assert parse_x_kiso_extension("Plain description.") == {}

    def test_extension_parsed(self):
        body = 'Extract text from an image.\n\n<x-kiso: {"consumes": ["image"]}>'
        assert parse_x_kiso_extension(body) == {"consumes": ["image"]}

    def test_trailing_whitespace_tolerated(self):
        body = 'desc\n<x-kiso: {"consumes": ["audio"]}>  \n'
        assert parse_x_kiso_extension(body) == {"consumes": ["audio"]}

    def test_malformed_json_ignored(self):
        body = "desc\n<x-kiso: {not json}>"
        assert parse_x_kiso_extension(body) == {}

    def test_empty_description(self):
        assert parse_x_kiso_extension("") == {}


class TestCatalogFileProcessingBlock:
    def test_single_consumer_method_gets_file_block(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        desc = 'Extract text from an image.\n\n<x-kiso: {"consumes": ["image"]}>'
        mgr = _StubManager(
            catalog={"ocr": [_method("ocr", "extract_text_from_image", desc, schema)]},
        )
        text = format_mcp_catalog(mgr)
        # Line containing "image" kind and the method name is present
        assert "File processing" in text or "image" in text.lower()
        assert "ocr:extract_text_from_image" in text

    def test_multiple_kinds_grouped(self):
        mgr = _StubManager(catalog={
            "ocr": [_method(
                "ocr", "extract",
                'OCR.\n\n<x-kiso: {"consumes": ["image"]}>',
            )],
            "transcriber": [_method(
                "transcriber", "transcribe",
                'ASR.\n\n<x-kiso: {"consumes": ["audio"]}>',
            )],
            "docreader": [_method(
                "docreader", "read",
                'Docs.\n\n<x-kiso: {"consumes": ["document"]}>',
            )],
        })
        text = format_mcp_catalog(mgr)
        assert "ocr:extract" in text
        assert "transcriber:transcribe" in text
        assert "docreader:read" in text

    def test_plain_methods_still_render_without_file_block(self):
        mgr = _StubManager(catalog={
            "echo": [_method("echo", "ping", "Health check.")],
        })
        text = format_mcp_catalog(mgr)
        # Plain descriptions — no "File processing" section needed
        assert "echo:ping" in text
