"""Security utilities — exec deny list, secret sanitization, fencing, permissions."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote

from kiso.config import Config, LLM_API_KEY_ENV

log = logging.getLogger(__name__)


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


# Target patterns: /, ~, $HOME (bare or with trailing /)
_ROOT_TARGET = r"\s+(/\s*$|~/?\s*$|\$HOME/?\s*$)"

DENY_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\brm\s+.*-[^\s]*r[^\s]*f" + _ROOT_TARGET), "rm -rf targeting / or ~ or $HOME",
     "Specify the exact path to delete, never use / or ~"),
    (re.compile(r"\brm\s+.*-[^\s]*f[^\s]*r" + _ROOT_TARGET), "rm -rf targeting / or ~ or $HOME",
     "Specify the exact path to delete, never use / or ~"),
    # rm -r without -f also dangerous when targeting root/home
    (re.compile(r"\brm\s+.*-[^\s]*r\b" + _ROOT_TARGET), "rm -r targeting / or ~ or $HOME",
     "Specify the exact path to delete, never use / or ~"),
    (re.compile(r"\bdd\s+.*\bif="), "dd if= (disk write)", ""),
    (re.compile(r"\bmkfs\b"), "mkfs (format filesystem)", ""),
    (re.compile(r"\bchmod\s+.*-R\s+777" + _ROOT_TARGET), "chmod -R 777 targeting / or ~ or $HOME",
     "Specify the exact path, never use / or ~"),
    (re.compile(r"\bchown\s+.*-R\b"), "chown -R (recursive ownership change)", ""),
    (re.compile(r"\b(shutdown|reboot)\b"), "shutdown/reboot", ""),
    # Fork bomb: classic :(){ :|:& } and named-function variants
    (re.compile(r":\(\)\s*\{.*\|.*&"), "fork bomb", ""),
    (re.compile(r"\b\w+\(\)\s*\{[^}]*\|\s*\w+[^}]*&"), "fork bomb (named function variant)", ""),
    (re.compile(r"base64.*\|\s*(sh|bash|zsh)"), "base64 decode piped to shell", ""),
    (re.compile(r"\bpython[23]?\s+-c\b"), "python one-liner execution",
     "Write a script file instead: cat > script.py << 'PYEOF'\n...\nPYEOF\npython3 script.py"),
    (re.compile(r"\bperl\s+-e\b"), "perl one-liner execution",
     "Write a script file instead: cat > script.pl << 'PLEOF'\n...\nPLEOF\nperl script.pl"),
    (re.compile(r"\bruby\s+-e\b"), "ruby one-liner execution",
     "Write a script file instead: cat > script.rb << 'RBEOF'\n...\nRBEOF\nruby script.rb"),
    (re.compile(r"\beval\b"), "eval command (indirect execution)",
     "Run the command directly without eval"),
    (re.compile(r"\bnode\s+-e\b"), "node one-liner execution",
     "Write a script file instead: cat > script.js << 'JSEOF'\n...\nJSEOF\nnode script.js"),
    # Protect kiso config files from direct shell writes (use 'kiso env set' instead)
    (re.compile(r">{1,2}\s*['\"]?[^|;&\n]*\.kiso[/\\]\.env\b"), "direct write to .kiso/.env",
     "Use 'kiso env set KEY VALUE' instead"),
    (re.compile(r">{1,2}\s*['\"]?[^|;&\n]*\.kiso[/\\]config\.toml\b"), "direct write to .kiso/config.toml",
     "Use the kiso CLI to manage configuration"),
]

_SUBSHELL_RE = re.compile(r"\$\(([^)]+)\)|`([^`]+)`")


def check_command_deny_list(command: str) -> str | None:
    """Returns denial reason string if blocked, None if allowed.

    Checks the full command first (for patterns that span metacharacters,
    e.g. fork bombs), then splits on shell metacharacters (;, |, ||, &&,
    newlines) and extracts $(...) / backtick contents to catch chained or
    substituted dangerous commands like ``echo | rm -rf /``.
    """
    # Check the full command first (catches patterns spanning metacharacters, e.g. fork bombs)
    for pattern, description, hint in DENY_PATTERNS:
        if pattern.search(command):
            msg = f"Command blocked: {description}"
            return f"{msg}. Hint: {hint}" if hint else msg
    # Split on shell metacharacters to catch piped/chained dangerous commands
    segments = re.split(r"\s*(?:;|\|{1,2}|&&|\n)\s*", command)
    # Also extract contents of $(...) and backtick substitutions
    for m in _SUBSHELL_RE.finditer(command):
        segments.append(m.group(1) or m.group(2))
    for segment in segments:
        for pattern, description, hint in DENY_PATTERNS:
            if pattern.search(segment):
                msg = f"Command blocked: {description}"
                return f"{msg}. Hint: {hint}" if hint else msg
    return None


# --- Secret sanitization ---


def build_secret_variants(value: str) -> list[str]:
    """Plaintext + base64 + URL-encoded + JSON-escaped. Skip values < 4 chars."""
    if not value or len(value) < 4:
        return []
    variants = {value}
    try:
        variants.add(base64.b64encode(value.encode()).decode())
    except Exception as exc:
        log.error("build_secret_variants: base64 encoding failed: %s", exc)
    encoded = quote(value, safe="")
    if encoded != value:
        variants.add(encoded)
    # JSON string escaping (catches secrets inside JSON output)
    json_escaped = json.dumps(value)[1:-1]  # strip surrounding quotes
    if json_escaped != value:
        variants.add(json_escaped)
    return list(variants)


_sanitize_cache: tuple[frozenset[str], re.Pattern[str] | None] = (frozenset(), None)


def sanitize_output(
    output: str,
    deploy_secrets: dict[str, str],
    ephemeral_secrets: dict[str, str],
) -> str:
    """Strip known secret values from output (plaintext, base64, URL-encoded, JSON-escaped).

    compile a single regex from all variants for a single-pass replacement.
    cache compiled pattern — recompile only when secret values change.
    """
    global _sanitize_cache  # noqa: PLW0603
    all_values = frozenset(
        list(deploy_secrets.values()) + list(ephemeral_secrets.values()),
    )
    if not all_values:
        return output

    cache_key, cached_pattern = _sanitize_cache
    if cache_key == all_values and cached_pattern is not None:
        return cached_pattern.sub("[REDACTED]", output)

    all_variants: list[str] = []
    for v in all_values:
        all_variants.extend(build_secret_variants(v))
    if not all_variants:
        return output
    # Sort longest-first so greedy alternation prefers longer matches
    all_variants.sort(key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in all_variants))
    _sanitize_cache = (all_values, pattern)
    return pattern.sub("[REDACTED]", output)


def sanitize_value(
    value: object,
    deploy_secrets: dict[str, str],
    ephemeral_secrets: dict[str, str],
) -> object:
    """Recursively sanitize strings inside structured values.

    Preserves the input container shape for common JSON-compatible values so
    runtime code can keep structured args/results in memory without first
    coercing them back into prompt-era strings.
    """
    if isinstance(value, str):
        return sanitize_output(value, deploy_secrets, ephemeral_secrets)
    if isinstance(value, dict):
        return {
            key: sanitize_value(item, deploy_secrets, ephemeral_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_value(item, deploy_secrets, ephemeral_secrets)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            sanitize_value(item, deploy_secrets, ephemeral_secrets)
            for item in value
        )
    return value


def collect_deploy_secrets() -> dict[str, str]:
    """Collect KISO_WRAPPER_*, KISO_CONNECTOR_* env vars + LLM API key."""
    secrets: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith(("KISO_WRAPPER_", "KISO_CONNECTOR_")):
            secrets[k] = v
    val = os.environ.get(LLM_API_KEY_ENV)
    if val:
        secrets[LLM_API_KEY_ENV] = val
    return secrets


# --- Permission re-validation ---


@dataclass
class PermissionResult:
    allowed: bool
    reason: str = ""
    role: str = ""


def revalidate_permissions(
    config: Config,
    username: str | None,
    task_type: str,
) -> PermissionResult:
    """Re-check user permissions against current config."""
    if username is None:
        return PermissionResult(allowed=True, role="admin")

    user = config.users.get(username)
    if user is None:
        return PermissionResult(
            allowed=False,
            reason=f"User '{username}' no longer exists in config",
        )

    return PermissionResult(allowed=True, role=user.role)
