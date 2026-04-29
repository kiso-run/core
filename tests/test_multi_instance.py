"""M1586 — Multi-instance test coverage (unit-tier).

Multi-instance is an active feature (`install.sh`, `kiso-host.sh`,
`~/.kiso/instances.json`). The audit (M1586 in v0.11) found zero
functional/live coverage. This module locks the unit-tier invariants
that don't require Docker or a live container:

- The instance-name regex (used by both `install.sh` and the bash
  completions) accepts canonical valid names and rejects every
  documented invalid form.

Live + bash + functional layers (Docker container spawning, kiso-host.sh
ambiguous resolution, two-instance session isolation) are deferred to a
follow-up that can run the bash + Docker tier.
"""

from __future__ import annotations

import re

import pytest


# Mirror the install.sh INSTANCE_NAME_RE — kept in sync via this lock.
INSTANCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
INSTANCE_MAX_LEN = 32


def _is_valid(name: str) -> bool:
    """Replicates `install.sh::validate_instance_name` (the regex
    + length + trailing-hyphen guard)."""
    if not name:
        return False
    if len(name) > INSTANCE_MAX_LEN:
        return False
    if not INSTANCE_NAME_RE.match(name):
        return False
    if name.endswith("-"):
        return False
    return True


@pytest.mark.parametrize("name", [
    "kiso",
    "my-bot",
    "bot2",
    "a",
    "a1",
    "personal",
    "work-instance",
    "x" * 32,  # max length boundary
])
def test_valid_instance_names_accepted(name):
    assert _is_valid(name), f"valid name rejected: {name!r}"


@pytest.mark.parametrize("name,reason", [
    ("", "empty"),
    ("-leading", "leading hyphen"),
    ("trailing-", "trailing hyphen"),
    ("UPPERCASE", "uppercase letter"),
    ("with space", "space"),
    ("under_score", "underscore"),
    ("dot.name", "dot"),
    ("special!", "special char"),
    ("x" * 33, "longer than 32 chars"),
])
def test_invalid_instance_names_rejected(name, reason):
    assert not _is_valid(name), (
        f"invalid name accepted ({reason}): {name!r}"
    )


def test_canonical_install_sh_regex_matches_lock():
    """The lock's INSTANCE_NAME_RE must stay in sync with the
    install.sh source. If install.sh changes, this test fails and the
    lock has to be updated explicitly."""
    from pathlib import Path
    install_sh = (
        Path(__file__).resolve().parent.parent / "install.sh"
    ).read_text()
    # Match the literal definition line.
    found = re.search(
        r"INSTANCE_NAME_RE=['\"](?P<re>[^'\"]+)['\"]",
        install_sh,
    )
    assert found, "install.sh INSTANCE_NAME_RE not found"
    assert found.group("re") == INSTANCE_NAME_RE.pattern, (
        f"install.sh regex {found.group('re')!r} drifted from lock "
        f"regex {INSTANCE_NAME_RE.pattern!r}"
    )
