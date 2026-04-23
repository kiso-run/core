"""Render an MCP ``tools/call`` result into a shape kiso consumers expect.

The MCP spec allows tool results to contain heterogeneous content:
text, image (base64), audio (base64), resource links, embedded
resources (text or blob), and an optional structured content
object. Kiso's reviewer and messenger both read task output as a
flat string, so we normalise the mixed content into:

- ``stdout_text``: concatenated text + structured content, plus the
  standard ``Published files:`` marker for any binary artifacts we
  saved. Downstream consumers (reviewer, messenger, cross-plan
  file awareness) read this as if it were a wrapper stdout.
- ``published_files``: list of ``(name, absolute_path)`` tuples for
  binary content written into the session workspace's ``pub/``
  directory. Kiso's existing pub serving machinery turns these
  into URLs automatically at delivery time.
- ``structured_content``: the raw structuredContent dict (if any)
  preserved verbatim for consumers that want typed access.
- ``is_error``: True when the MCP server flagged ``isError`` on
  the response.

Binary content goes to ``sessions/<session>/pub/mcp-<server>-<method>
-<task_id>-<idx>.<ext>`` so it ends up URL-addressable alongside
the artifacts produced by traditional wrappers.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from pathlib import Path

from kiso.mcp.schemas import MCPCallResult, MCPResourceContent

log = logging.getLogger(__name__)

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
}


def _ext_for_mime(mime: str | None) -> str:
    if not mime or not isinstance(mime, str):
        return "bin"
    return _MIME_EXT.get(mime.lower().split(";", 1)[0].strip(), "bin")


def render_mcp_result(
    server: str,
    method: str,
    task_id: int | str,
    session_pub_dir: Path | None,
    result: dict,
) -> MCPCallResult:
    """Turn a raw MCP ``tools/call`` result into a ``MCPCallResult``.

    *session_pub_dir* is the per-session pub directory (typically
    ``~/.kiso/sessions/<session>/pub``); when binary content is
    present we create the directory on demand and write files into
    it. Passing ``None`` means "don't materialise binary content on
    disk" — callers that don't care about binaries (CLI dry-runs,
    tests that mock the filesystem) can opt out.

    Raises nothing: all failures to render an individual content
    item are logged and swallowed so one bad block does not poison
    the whole response. A completely empty content list yields
    ``stdout_text=""`` and ``is_error=False``.
    """
    content = result.get("content") or []
    structured = result.get("structuredContent")
    is_error = bool(result.get("isError"))

    lines: list[str] = []
    published: list[tuple[str, Path]] = []

    # Collect the text that appears alongside structured content so we
    # can dedupe if the text blocks are just the JSON-serialized form
    # of structuredContent (per the MCP spec backwards-compat note).
    structured_json: str | None = None
    if structured is not None:
        try:
            structured_json = json.dumps(structured, sort_keys=True)
        except (TypeError, ValueError):
            structured_json = None

    idx = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text":
            text = str(item.get("text", ""))
            if (
                structured_json is not None
                and text.strip()
                and _looks_like_json_of(text, structured)
            ):
                # Duplicate of structured content → drop the text to
                # save tokens for the reviewer.
                continue
            lines.append(text)
        elif itype in ("image", "audio"):
            saved = _write_binary_block(
                server, method, task_id, idx, item, session_pub_dir
            )
            if saved is not None:
                published.append(saved)
                name = saved[0]
                lines.append(f"[{itype} saved: {name}]")
            else:
                mime = item.get("mimeType", "?")
                lines.append(f"[{itype} content: {mime} — not written to disk]")
            idx += 1
        elif itype == "resource_link":
            uri = item.get("uri", "")
            name = item.get("name") or ""
            desc = item.get("description") or ""
            suffix = f" ({desc})" if desc else ""
            label = name or uri
            lines.append(f"[resource link: {label}{suffix}] {uri}".strip())
        elif itype == "resource":
            res = item.get("resource") or {}
            uri = res.get("uri", "")
            mime = res.get("mimeType", "")
            text_body = res.get("text")
            blob = res.get("blob")
            if isinstance(text_body, str):
                header = f"[embedded resource: {uri}"
                if mime:
                    header += f" ({mime})"
                header += "]"
                lines.append(f"{header}\n{text_body}")
            elif isinstance(blob, str):
                saved = _write_blob_block(
                    server, method, task_id, idx, res, session_pub_dir
                )
                if saved is not None:
                    published.append(saved)
                    name = saved[0]
                    lines.append(f"[embedded resource saved: {name}] {uri}".strip())
                else:
                    lines.append(f"[embedded resource: {uri} ({mime}) — not written]")
                idx += 1
            else:
                lines.append(f"[embedded resource: {uri}]")
        else:
            lines.append(f"[content of type {itype!r}]")

    # Append the structured content as the last "ground truth" line
    # so the reviewer and messenger see exactly what the server
    # returned, even when the text blocks above omit it.
    if structured is not None and structured_json is not None:
        lines.append(structured_json)

    stdout_text = "\n".join(line for line in lines if line is not None)

    if published:
        stdout_text = _append_pub_marker(stdout_text, published, session_pub_dir)

    return MCPCallResult(
        stdout_text=stdout_text,
        published_files=[(name, str(path)) for name, path in published],
        structured_content=structured if isinstance(structured, dict) else None,
        is_error=is_error,
    )


def render_mcp_resource_result(
    server: str,
    uri: str,
    task_id: int | str,
    session_pub_dir: Path | None,
    blocks: list[MCPResourceContent],
) -> MCPCallResult:
    """Turn a list of resource content blocks into an ``MCPCallResult``.

    Text blocks are concatenated into ``stdout_text`` prefixed with a
    ``[resource: <uri>]`` header so the reviewer sees which URI it was.
    Binary blocks (``blob`` set) are base64-decoded and written to
    *session_pub_dir* using the same naming scheme as ``render_mcp_result``;
    the standard ``Published files:`` marker block is appended so the
    pub-serving machinery picks them up.
    """
    lines: list[str] = []
    published: list[tuple[str, Path]] = []
    idx = 0
    for block in blocks:
        block_uri = block.uri or uri
        mime = (block.mime_type or "").strip()
        if block.text is not None:
            header = f"[resource: {block_uri}"
            if mime:
                header += f" ({mime})"
            header += "]"
            lines.append(f"{header}\n{block.text}")
        elif block.blob is not None:
            saved = _write_resource_blob(
                server, uri, task_id, idx, block, session_pub_dir
            )
            if saved is not None:
                published.append(saved)
                name = saved[0]
                tail = f" ({mime})" if mime else ""
                lines.append(f"[resource saved: {block_uri}{tail}] {name}")
            else:
                tail = f" ({mime})" if mime else ""
                lines.append(
                    f"[resource: {block_uri}{tail} — blob not written]"
                )
            idx += 1
        else:
            tail = f" ({mime})" if mime else ""
            lines.append(f"[resource: {block_uri}{tail} — empty block]")

    stdout_text = "\n".join(lines)
    if published:
        stdout_text = _append_pub_marker(stdout_text, published, session_pub_dir)
    return MCPCallResult(
        stdout_text=stdout_text,
        published_files=[(name, str(path)) for name, path in published],
        structured_content=None,
        is_error=False,
    )


def _write_resource_blob(
    server: str,
    uri: str,
    task_id: int | str,
    idx: int,
    block: MCPResourceContent,
    pub_dir: Path | None,
) -> tuple[str, Path] | None:
    if pub_dir is None:
        return None
    blob_b64 = block.blob
    if not isinstance(blob_b64, str) or not blob_b64:
        return None
    try:
        raw = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    ext = _ext_for_mime(block.mime_type)
    # Reuse the same filename helper used by tool results to keep pub/
    # naming consistent. ``method`` is the synthetic __resource_read.
    name = _binary_filename(server, "__resource_read", task_id, idx, ext)
    try:
        pub_dir.mkdir(parents=True, exist_ok=True)
        out_path = pub_dir / name
        out_path.write_bytes(raw)
    except OSError:
        return None
    return (name, out_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _looks_like_json_of(text: str, structured: object) -> bool:
    """True when *text* is the JSON-serialized form of *structured*."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return False
    return parsed == structured


def _binary_filename(
    server: str, method: str, task_id: int | str, idx: int, ext: str
) -> str:
    # Sanitise server/method to filesystem-safe characters; fall back to
    # "unknown" if the input degenerates to empty.
    def _clean(s: str) -> str:
        cleaned = "".join(
            c if c.isalnum() or c in ("-", "_") else "-" for c in (s or "")
        )
        return cleaned.strip("-") or "unknown"

    return f"mcp-{_clean(server)}-{_clean(method)}-{task_id}-{idx}.{ext}"


def _write_binary_block(
    server: str,
    method: str,
    task_id: int | str,
    idx: int,
    item: dict,
    pub_dir: Path | None,
) -> tuple[str, Path] | None:
    """Decode *item* (image or audio MCP content) and save to pub_dir.

    Returns (relative_name, absolute_path) on success, None if pub_dir
    is None, the data field is missing, or the base64 decode fails.
    """
    if pub_dir is None:
        return None
    data_b64 = item.get("data")
    if not isinstance(data_b64, str) or not data_b64:
        return None
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        log.warning(
            "render_mcp_result: base64 decode failed for %s %s idx=%d: %s",
            server, method, idx, e,
        )
        return None
    ext = _ext_for_mime(item.get("mimeType"))
    name = _binary_filename(server, method, task_id, idx, ext)
    try:
        pub_dir.mkdir(parents=True, exist_ok=True)
        out_path = pub_dir / name
        out_path.write_bytes(raw)
    except OSError as e:
        log.warning(
            "render_mcp_result: write failed for %s: %s", name, e
        )
        return None
    return (name, out_path)


def _write_blob_block(
    server: str,
    method: str,
    task_id: int | str,
    idx: int,
    resource: dict,
    pub_dir: Path | None,
) -> tuple[str, Path] | None:
    if pub_dir is None:
        return None
    blob_b64 = resource.get("blob")
    if not isinstance(blob_b64, str) or not blob_b64:
        return None
    try:
        raw = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    ext = _ext_for_mime(resource.get("mimeType"))
    name = _binary_filename(server, method, task_id, idx, ext)
    try:
        pub_dir.mkdir(parents=True, exist_ok=True)
        out_path = pub_dir / name
        out_path.write_bytes(raw)
    except OSError:
        return None
    return (name, out_path)


def _append_pub_marker(
    stdout_text: str,
    published: list[tuple[str, Path]],
    pub_dir: Path | None,
) -> str:
    """Append the standard ``Published files:`` marker block so the
    existing ``_extract_published_urls`` parser picks up the files
    identically to how it treats wrapper output."""
    if not published:
        return stdout_text
    lines = ["", "Published files:"]
    for name, _path in published:
        # URL is filled in by the pub-serving machinery later; we
        # emit the path-as-URL placeholder so the marker format
        # stays parseable.
        lines.append(f"- {name}: pub/{name}")
    suffix = "\n".join(lines)
    return f"{stdout_text}\n{suffix}" if stdout_text else suffix.lstrip("\n")
