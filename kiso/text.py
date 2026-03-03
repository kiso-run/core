"""Shared text-processing utilities."""

from __future__ import annotations

import re

_THINK_RE = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>",
    re.DOTALL,
)


def extract_thinking(text: str) -> tuple[str, str]:
    """Extract ``<think>``/``<thinking>`` blocks from *text*.

    Returns ``(thinking, clean_text)`` where *thinking* is the concatenated
    content of all matched blocks and *clean_text* is *text* with those blocks
    removed.
    """
    if "<think" not in text:
        return "", text
    blocks = []
    for m in _THINK_RE.finditer(text):
        blocks.append(m.group(1).strip())
    clean = _THINK_RE.sub("", text).strip()
    return "\n".join(blocks), clean
