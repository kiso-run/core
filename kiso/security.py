"""Security utilities — exec deny list, secret sanitization, fencing."""

from __future__ import annotations

import base64
import os
import re
import secrets
from urllib.parse import quote


def escape_fence_delimiters(content: str) -> str:
    """Escape <<<...>>> to prevent pre-crafted delimiters."""
    return content.replace("<<<", "«««").replace(">>>", "»»»")


def fence_content(content: str, label: str) -> str:
    """Wrap content with random boundary tokens."""
    token = secrets.token_hex(16)
    marker = f"{label}_{token}"
    escaped = escape_fence_delimiters(content)
    return f"<<<{marker}>>>\n{escaped}\n<<<END_{marker}>>>"


class CommandDeniedError(Exception):
    """Raised when a command matches the deny list."""


# Target patterns: bare /, ~, $HOME (with optional trailing /)
_ROOT_TARGET = r"\s+(/\s*$|~\s*$|\$HOME\s*$)"

DENY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+.*-[^\s]*r[^\s]*f" + _ROOT_TARGET), "rm -rf targeting / or ~ or $HOME"),
    (re.compile(r"\brm\s+.*-[^\s]*f[^\s]*r" + _ROOT_TARGET), "rm -rf targeting / or ~ or $HOME"),
    (re.compile(r"\bdd\s+.*\bif="), "dd if= (disk write)"),
    (re.compile(r"\bmkfs\b"), "mkfs (format filesystem)"),
    (re.compile(r"\bchmod\s+.*-R\s+777" + _ROOT_TARGET), "chmod -R 777 targeting / or ~ or $HOME"),
    (re.compile(r"\bchown\s+.*-R\b"), "chown -R (recursive ownership change)"),
    (re.compile(r"\b(shutdown|reboot)\b"), "shutdown/reboot"),
    (re.compile(r":\(\)\s*\{.*\|.*&"), "fork bomb"),
]


def check_command_deny_list(command: str) -> str | None:
    """Returns denial reason string if blocked, None if allowed."""
    for pattern, description in DENY_PATTERNS:
        if pattern.search(command):
            return f"Command blocked: {description}"
    return None


# --- Secret sanitization ---


def build_secret_variants(value: str) -> list[str]:
    """Plaintext + base64 + URL-encoded. Skip values < 4 chars."""
    if not value or len(value) < 4:
        return []
    variants = {value}
    try:
        variants.add(base64.b64encode(value.encode()).decode())
    except Exception:
        pass
    encoded = quote(value, safe="")
    if encoded != value:
        variants.add(encoded)
    return list(variants)


def sanitize_output(
    output: str,
    deploy_secrets: dict[str, str],
    ephemeral_secrets: dict[str, str],
) -> str:
    """Strip known secret values from output (plaintext, base64, URL-encoded)."""
    all_variants: list[str] = []
    for v in list(deploy_secrets.values()) + list(ephemeral_secrets.values()):
        all_variants.extend(build_secret_variants(v))
    all_variants.sort(key=len, reverse=True)  # longest first
    for variant in all_variants:
        output = output.replace(variant, "[REDACTED]")
    return output


def collect_deploy_secrets(config=None) -> dict[str, str]:
    """Collect KISO_SKILL_*, KISO_CONNECTOR_* env vars + provider API keys."""
    secrets: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith(("KISO_SKILL_", "KISO_CONNECTOR_")):
            secrets[k] = v
    if config:
        for prov in config.providers.values():
            if prov.api_key_env:
                val = os.environ.get(prov.api_key_env)
                if val:
                    secrets[prov.api_key_env] = val
    return secrets
