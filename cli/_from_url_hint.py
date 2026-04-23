"""Helper: detect the common mistake of running
``kiso skill install <url>`` or ``kiso mcp install <package>`` without
the ``--from-url`` flag, and produce a friendly hint.

argparse's native error is "the following arguments are required:
--from-url" which is accurate but not helpful for a user who typed a
perfectly valid install source but forgot the flag. Running the hint
check before argparse lets us upgrade the message.

The detector is deterministic and has no side effects: it returns
either a hint string (to be printed to stderr before ``exit(2)``) or
``None`` (no hint — argparse handles normally).
"""

from __future__ import annotations


_INSTALL_SOURCE_PREFIXES = (
    "http://",
    "https://",
    "file://",
    "git+",
    "git@",
    "npm:",
    "pypi:",
    "pulsemcp:",
)


def _looks_like_install_source(token: str) -> bool:
    return token.startswith(_INSTALL_SOURCE_PREFIXES)


def detect_missing_from_url(argv: list[str]) -> str | None:
    """Return a hint for missing ``--from-url`` on install commands.

    Matches ``kiso {skill,mcp} install <source>`` where ``<source>``
    looks like a URL / package-manager identifier but ``--from-url``
    is absent from the rest of the argv. Returns ``None`` if the call
    is well-formed or does not look like an install.
    """
    # Find the first non-kiso/global-flag token.
    if not argv:
        return None

    # Look for the literal subcommand pair: "skill install" or "mcp install".
    # We scan from left to right, skipping argv[0] (the executable) and any
    # tokens that start with "-" (global flags like --debug).
    tokens = [t for t in argv[1:] if not t.startswith("-")]
    if len(tokens) < 3:
        return None

    subsystem = tokens[0]
    action = tokens[1]
    if subsystem not in ("skill", "mcp") or action != "install":
        return None

    source_candidate = tokens[2]
    if not _looks_like_install_source(source_candidate):
        return None

    # The user passed a install-source-looking positional. If --from-url
    # is already in the raw argv, they're fine.
    for tok in argv:
        if tok == "--from-url" or tok.startswith("--from-url="):
            return None

    return (
        f"error: missing --from-url\n"
        f"  Did you mean: kiso {subsystem} install --from-url "
        f"{source_candidate}\n"
        f"  Accepted URL forms: github / file:// / npm: / pypi: / "
        f"pulsemcp: / server.json"
    )
