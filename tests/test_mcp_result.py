"""Unit tests for ``kiso.mcp.result.render_mcp_result``.

Covers the full content-type mapping:
- text → stdout
- structured content → stdout JSON with dedupe of duplicate text block
- image/audio/blob → workspace file + Published files: marker
- resource_link → stdout URL/path line
- resource embedded text → inlined with header
- isError → propagated
- empty content → empty stdout, not an error
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from kiso.mcp.result import render_mcp_result


def _tiny_png_b64() -> str:
    return base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c6300010000000500010d0a2db400000000"
            "49454e44ae426082"
        )
    ).decode()


class TestTextContent:
    def test_single_text_block(self, tmp_path):
        result = {"content": [{"type": "text", "text": "hello"}]}
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert out.stdout_text == "hello"
        assert out.published_files == []
        assert out.is_error is False

    def test_multiple_text_blocks_concatenated(self, tmp_path):
        result = {
            "content": [
                {"type": "text", "text": "line 1"},
                {"type": "text", "text": "line 2"},
            ]
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert out.stdout_text == "line 1\nline 2"

    def test_empty_content_is_not_an_error(self, tmp_path):
        out = render_mcp_result("s", "m", 1, tmp_path, {"content": []})
        assert out.stdout_text == ""
        assert out.is_error is False
        assert out.published_files == []


class TestStructuredContent:
    def test_structured_appears_as_json(self, tmp_path):
        result = {
            "content": [{"type": "text", "text": "rendered"}],
            "structuredContent": {"ok": True, "value": 42},
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert "rendered" in out.stdout_text
        assert json.dumps({"ok": True, "value": 42}, sort_keys=True) in out.stdout_text
        assert out.structured_content == {"ok": True, "value": 42}

    def test_duplicate_text_dedupe(self, tmp_path):
        """Per spec back-compat, servers often include a text block that
        is just the JSON-serialized form of structuredContent. We drop
        the duplicate to save tokens."""
        payload = {"ok": True}
        result = {
            "content": [
                {"type": "text", "text": json.dumps(payload, sort_keys=True)},
            ],
            "structuredContent": payload,
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        # Structured form appears exactly once
        assert out.stdout_text.count(json.dumps(payload, sort_keys=True)) == 1


class TestImageContent:
    def test_image_saved_to_pub_dir(self, tmp_path):
        result = {
            "content": [
                {
                    "type": "image",
                    "data": _tiny_png_b64(),
                    "mimeType": "image/png",
                }
            ]
        }
        out = render_mcp_result("github", "get_avatar", 42, tmp_path, result)
        assert len(out.published_files) == 1
        name, path_str = out.published_files[0]
        assert name == "mcp-github-get_avatar-42-0.png"
        assert Path(path_str).exists()
        assert Path(path_str).read_bytes().startswith(b"\x89PNG")
        # Published files marker in stdout
        assert "Published files:" in out.stdout_text
        assert name in out.stdout_text

    def test_image_without_pub_dir_falls_back_to_placeholder(self):
        result = {
            "content": [
                {
                    "type": "image",
                    "data": _tiny_png_b64(),
                    "mimeType": "image/png",
                }
            ]
        }
        out = render_mcp_result("s", "m", 1, None, result)
        assert out.published_files == []
        assert "image content" in out.stdout_text

    def test_invalid_base64_does_not_crash(self, tmp_path):
        result = {
            "content": [
                {"type": "image", "data": "not-base64!!", "mimeType": "image/png"}
            ]
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        # Falls back to placeholder, nothing published
        assert out.published_files == []
        assert "image content" in out.stdout_text


class TestAudioContent:
    def test_audio_saved(self, tmp_path):
        result = {
            "content": [
                {
                    "type": "audio",
                    "data": base64.b64encode(b"fakewav").decode(),
                    "mimeType": "audio/wav",
                }
            ]
        }
        out = render_mcp_result("s", "transcribe", 1, tmp_path, result)
        assert len(out.published_files) == 1
        name, _ = out.published_files[0]
        assert name.endswith(".wav")


class TestResourceLink:
    def test_file_uri_in_stdout(self, tmp_path):
        result = {
            "content": [
                {
                    "type": "resource_link",
                    "uri": "file:///tmp/example.py",
                    "name": "example.py",
                    "description": "the thing",
                }
            ]
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert "example.py" in out.stdout_text
        assert "file:///tmp/example.py" in out.stdout_text

    def test_http_uri_in_stdout_no_fetch(self, tmp_path):
        result = {
            "content": [
                {
                    "type": "resource_link",
                    "uri": "https://example.com/doc",
                    "name": "doc",
                }
            ]
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert "https://example.com/doc" in out.stdout_text


class TestEmbeddedResource:
    def test_text_embedded(self, tmp_path):
        result = {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/readme.md",
                        "mimeType": "text/markdown",
                        "text": "# Hello",
                    },
                }
            ]
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert "# Hello" in out.stdout_text
        assert "file:///tmp/readme.md" in out.stdout_text

    def test_blob_embedded_saved(self, tmp_path):
        result = {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "memory://img",
                        "mimeType": "image/png",
                        "blob": _tiny_png_b64(),
                    },
                }
            ]
        }
        out = render_mcp_result("s", "m", 7, tmp_path, result)
        assert len(out.published_files) == 1
        name, path_str = out.published_files[0]
        assert name.endswith(".png")
        assert Path(path_str).exists()


class TestIsError:
    def test_is_error_propagated(self, tmp_path):
        result = {
            "content": [{"type": "text", "text": "rate limit exceeded"}],
            "isError": True,
        }
        out = render_mcp_result("s", "m", 1, tmp_path, result)
        assert out.is_error is True
        assert "rate limit" in out.stdout_text


class TestMixedContent:
    def test_text_plus_image(self, tmp_path):
        result = {
            "content": [
                {"type": "text", "text": "here is the avatar"},
                {
                    "type": "image",
                    "data": _tiny_png_b64(),
                    "mimeType": "image/png",
                },
            ]
        }
        out = render_mcp_result("gh", "avatar", 5, tmp_path, result)
        assert "here is the avatar" in out.stdout_text
        assert len(out.published_files) == 1
        assert "Published files:" in out.stdout_text


class TestFilenameSanitization:
    def test_server_with_special_chars(self, tmp_path):
        """Server/method names with path-unsafe characters are sanitised."""
        result = {
            "content": [
                {
                    "type": "image",
                    "data": _tiny_png_b64(),
                    "mimeType": "image/png",
                }
            ]
        }
        out = render_mcp_result("../weird", "m/slash", 1, tmp_path, result)
        name, _ = out.published_files[0]
        # No '/' or '..' in the filename
        assert "/" not in name
        assert ".." not in name
