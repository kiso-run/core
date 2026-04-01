"""tool-ocr — extract text from images via Gemini multimodal OCR.

Subprocess contract (same as all kiso tools):
  stdin:  JSON {args, session, workspace, session_secrets, plan_outputs}
  stdout: result text on success
  stderr: error description on failure
  exit 0: success, exit 1: failure

Uses Gemini 2.5 Flash via OpenRouter's /chat/completions endpoint.
Images are sent as base64 inline content. Same API key as all kiso LLM calls.

Cost: ~260 tokens per image (A4 page equivalent) → $0.00004/image at Gemini Flash pricing.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import signal
import sys
import time
import unicodedata
from pathlib import Path

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

_MAX_OUTPUT_CHARS = 50_000
_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB — Gemini inline image limit


def _has_meaningful_content(text: str, min_chars: int = 3) -> bool:
    """Check if text has at least *min_chars* printable characters.

    Counts Unicode letters (L*), numbers (N*), and punctuation (P*).
    Zero-width characters (U+200B, U+FEFF, etc.) are category Cf and
    are NOT counted, so invisible-only text returns False.
    """
    count = sum(1 for c in text if unicodedata.category(c)[0] in ("L", "N", "P"))
    return count >= min_chars
_EMPTY_RETRIES = 2  # retry up to 2 times on empty API response
_RETRY_BACKOFF = (1, 2)  # seconds between retries

_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif",
})

_EXTRACT_PROMPT = (
    "Extract ALL text from this image exactly as written. "
    "Preserve the original layout, line breaks, and formatting as much as possible. "
    "Return only the extracted text, no commentary or description."
)

_DESCRIBE_PROMPT = (
    "Describe what is in this image. Include: main subject, text content if any, "
    "layout, colors, and any notable visual elements. Be concise but thorough."
)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)
    args = data.get("args", {})
    workspace = data.get("workspace", ".")

    action = args.get("action", "extract")

    try:
        if action == "list":
            result = do_list(workspace)
        elif action == "info":
            result = do_info(workspace, args)
        elif action == "extract":
            result = do_extract(workspace, args)
        elif action == "describe":
            result = do_describe(workspace, args)
        else:
            print(f"Unknown action: {action}", file=sys.stderr)
            sys.exit(1)
    except FileNotFoundError as e:
        print(f"File not found: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(result)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def do_list(workspace: str) -> str:
    """List image files in the uploads/ directory."""
    uploads = Path(workspace) / "uploads"
    if not uploads.is_dir():
        return "No uploads/ directory found."
    files = sorted(
        f for f in uploads.rglob("*")
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTENSIONS
    )
    if not files:
        return "No image files found in uploads/."
    lines = [f"Image files in uploads/ ({len(files)}):"]
    for f in files:
        rel = f.relative_to(uploads)
        size = f.stat().st_size
        dims = _get_dimensions(f)
        dim_str = f", {dims[0]}x{dims[1]}" if dims else ""
        lines.append(f"  {rel} ({_format_size(size)}{dim_str})")
    return "\n".join(lines)


def do_info(workspace: str, args: dict) -> str:
    """Get image file metadata without processing."""
    file_path = _resolve_path(workspace, args)
    size = file_path.stat().st_size
    dims = _get_dimensions(file_path)
    lines = [
        f"File: {file_path.name}",
        f"Size: {_format_size(size)}",
        f"Format: {file_path.suffix.lower()}",
    ]
    if dims:
        lines.append(f"Dimensions: {dims[0]}x{dims[1]}")
    return "\n".join(lines)


def do_extract(workspace: str, args: dict) -> str:
    """Extract text from an image via Gemini OCR."""
    file_path = _resolve_path(workspace, args)
    _check_file_size(file_path)

    api_key = _get_api_key()
    text = _call_gemini(file_path, api_key, _EXTRACT_PROMPT)

    header = f"OCR: {file_path.name}"
    dims = _get_dimensions(file_path)
    if dims:
        header += f" ({dims[0]}x{dims[1]})"

    if not _has_meaningful_content(text):
        return f"{header}\nNo text detected in image."

    if len(text) > _MAX_OUTPUT_CHARS:
        shown = text[:_MAX_OUTPUT_CHARS]
        last_nl = shown.rfind("\n")
        if last_nl > 0:
            shown = shown[:last_nl]
        text = (
            f"{shown}\n\n"
            f"Showing first {len(shown)} of {len(text)} chars."
        )

    return f"{header}\n\n{text}"


def do_describe(workspace: str, args: dict) -> str:
    """Describe the contents of an image."""
    file_path = _resolve_path(workspace, args)
    _check_file_size(file_path)

    api_key = _get_api_key()
    description = _call_gemini(file_path, api_key, _DESCRIBE_PROMPT)

    header = f"Description: {file_path.name}"
    return f"{header}\n\n{description}"


# ---------------------------------------------------------------------------
# Gemini API
# ---------------------------------------------------------------------------


def _get_api_key() -> str:
    """Get API key — same as kiso LLM key."""
    key = os.environ.get("KISO_LLM_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "No API key found. Set KISO_LLM_API_KEY (same key used by kiso for all LLM calls)."
    )


def _call_gemini(file_path: Path, api_key: str, prompt: str) -> str:
    """Send image to Gemini via OpenRouter chat completion."""
    import httpx

    base_url = os.environ.get(
        "KISO_TOOL_OCR_BASE_URL",
        "https://openrouter.ai/api/v1",
    )
    url = f"{base_url}/chat/completions"

    image_data = base64.b64encode(file_path.read_bytes()).decode()
    mime_type = mimetypes.guess_type(str(file_path))[0] or "image/png"

    payload = {
        "model": "google/gemini-2.5-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_data}",
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            },
        ],
        "max_tokens": 8192,
        "temperature": 0,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(_EMPTY_RETRIES + 1):
        response = httpx.post(url, headers=headers, json=payload, timeout=120)

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API error ({response.status_code}): {response.text[:500]}"
            )

        result = response.json()
        choices = result.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") or ""

        # Reasoning→content fallback: some models route text to "reasoning"
        # instead of "content" when the reasoning parameter was used. Guard
        # against that so extraction still works even if the API misbehaves.
        if not _has_meaningful_content(content):
            reasoning = message.get("reasoning", "") or ""
            if _has_meaningful_content(reasoning):
                print("[ocr-diag] WARNING: reasoning-fallback used — content empty, using reasoning field", file=sys.stderr)
                content = reasoning

        if _has_meaningful_content(content):
            return content

        # Log diagnostic info for empty responses
        _diag = {
            "attempt": attempt + 1,
            "model": result.get("model", "?"),
            "choices_len": len(choices),
            "finish_reason": choices[0].get("finish_reason") if choices else None,
            "usage": result.get("usage"),
        }
        print(f"[ocr-diag] Empty response: {_diag}", file=sys.stderr)

        # Empty response — retry with backoff if attempts remain
        if attempt < _EMPTY_RETRIES:
            time.sleep(_RETRY_BACKOFF[attempt])

    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(workspace: str, args: dict) -> Path:
    """Resolve file_path arg to an absolute Path."""
    file_path = args.get("file_path")
    if not file_path:
        raise ValueError("file_path argument is required for extract/describe/info actions")
    resolved = (Path(workspace) / file_path).resolve()
    ws_resolved = Path(workspace).resolve()
    try:
        resolved.relative_to(ws_resolved)
    except ValueError:
        raise ValueError(f"Path traversal denied: {file_path}")
    if not resolved.is_file():
        raise FileNotFoundError(resolved.name)
    return resolved


def _check_file_size(path: Path) -> None:
    """Reject files exceeding the size limit."""
    size = path.stat().st_size
    if size > _MAX_FILE_SIZE:
        raise ValueError(
            f"File too large ({_format_size(size)}). "
            f"Limit is {_format_size(_MAX_FILE_SIZE)}."
        )


def _get_dimensions(path: Path) -> tuple[int, int] | None:
    """Get image dimensions without heavy dependencies."""
    try:
        # PNG: width/height at bytes 16-24
        data = path.read_bytes()[:32]
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h)
        # JPEG: scan for SOF0 marker
        if data[:2] == b"\xff\xd8":
            return _jpeg_dimensions(path)
    except Exception:
        pass
    return None


def _jpeg_dimensions(path: Path) -> tuple[int, int] | None:
    """Extract JPEG dimensions from SOF markers."""
    data = path.read_bytes()
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2:i + 4], "big")
        # SOF0, SOF1, SOF2 markers
        if marker in (0xC0, 0xC1, 0xC2):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h)
        i += 2 + length
    return None


def _format_size(size: int) -> str:
    """Format byte size as human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


if __name__ == "__main__":
    main()
