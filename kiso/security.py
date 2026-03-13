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
    # M506: JSON string escaping (catches secrets inside JSON output)
    json_escaped = json.dumps(value)[1:-1]  # strip surrounding quotes
    if json_escaped != value:
        variants.add(json_escaped)
    return list(variants)


def sanitize_output(
    output: str,
    deploy_secrets: dict[str, str],
    ephemeral_secrets: dict[str, str],
) -> str:
    """Strip known secret values from output (plaintext, base64, URL-encoded, JSON-escaped).

    M506: compile a single regex from all variants for a single-pass replacement.
    """
    all_variants: list[str] = []
    for v in list(deploy_secrets.values()) + list(ephemeral_secrets.values()):
        all_variants.extend(build_secret_variants(v))
    if not all_variants:
        return output
    # Sort longest-first so greedy alternation prefers longer matches
    all_variants.sort(key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in all_variants))
    return pattern.sub("[REDACTED]", output)


def collect_deploy_secrets() -> dict[str, str]:
    """Collect KISO_TOOL_*, KISO_CONNECTOR_* env vars + LLM API key."""
    secrets: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith(("KISO_TOOL_", "KISO_SKILL_", "KISO_CONNECTOR_")):
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
    tools: str | list[str] | None = None


def revalidate_permissions(
    config: Config,
    username: str | None,
    task_type: str,
    tool_name: str | None = None,
) -> PermissionResult:
    """Re-check user permissions against current config."""
    if username is None:
        # System/anonymous — allow (trusted at entry)
        return PermissionResult(allowed=True, role="admin")

    user = config.users.get(username)
    if user is None:
        return PermissionResult(
            allowed=False,
            reason=f"User '{username}' no longer exists in config",
        )

    # search tasks are safe (no shell execution, no sandbox) — always allowed
    if task_type == "search":
        return PermissionResult(allowed=True, role=user.role, tools=user.tools)

    if task_type in ("skill", "tool") and tool_name and user.role == "user":
        if user.tools != "*":
            if tool_name not in (user.tools or []):
                return PermissionResult(
                    allowed=False,
                    reason=f"Tool '{tool_name}' not in user's allowed tools",
                )

    return PermissionResult(allowed=True, role=user.role, tools=user.tools)
