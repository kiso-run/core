"""Concern 1 — trust prefix matching must anchor at segment boundaries.

A prefix like ``github.com/anthropic*`` must not match
``github.com/anthropic-fake/malicious``. The user's intent with a
trust prefix is always "this owner / this path", never "any string
starting with these characters".

Matches must happen at path segment boundaries (``/``), never at
arbitrary character positions inside a segment.
"""

from __future__ import annotations

import pytest

from kiso.trust_store import matches_any_prefix


class TestGlobSegmentAnchoring:
    def test_owner_glob_does_not_match_lookalike_owner(self):
        # github.com/anthropic* should cover anthropic/foo but NOT
        # anthropic-fake/foo — '-' is not a segment boundary.
        assert (
            matches_any_prefix(
                "github.com/anthropic-fake/malicious",
                ["github.com/anthropic*"],
            )
            is False
        )

    def test_owner_glob_matches_real_owner(self):
        assert (
            matches_any_prefix(
                "github.com/anthropic/skills",
                ["github.com/anthropic*"],
            )
            is True
        )

    def test_npm_scope_glob_does_not_match_lookalike_scope(self):
        assert (
            matches_any_prefix(
                "npm:@modelcontextprotocol-fake/server-evil",
                ["npm:@modelcontextprotocol*"],
            )
            is False
        )

    def test_npm_scope_glob_matches_real_scope_package(self):
        assert (
            matches_any_prefix(
                "npm:@modelcontextprotocol/server-fs",
                ["npm:@modelcontextprotocol*"],
            )
            is True
        )

    def test_bare_prefix_anchors_on_segment(self):
        # No glob, trailing slash or not — must never match a
        # neighbouring owner whose name happens to share the same prefix.
        assert (
            matches_any_prefix(
                "github.com/kiso-runabc/evil",
                ["github.com/kiso-run/"],
            )
            is False
        )
        assert (
            matches_any_prefix(
                "github.com/kiso-run/core",
                ["github.com/kiso-run/"],
            )
            is True
        )

    def test_bare_prefix_matches_exact_key(self):
        # Source that matches the prefix exactly (no children) should match.
        assert (
            matches_any_prefix("github.com/kiso-run", ["github.com/kiso-run/"])
            is True
        )

    def test_segment_glob_after_slash_is_standard_path_prefix(self):
        # "github.com/anthropics/skills/*" is the canonical "this subtree"
        # form — everything below the third segment matches.
        assert (
            matches_any_prefix(
                "github.com/anthropics/skills/writing-style",
                ["github.com/anthropics/skills/*"],
            )
            is True
        )
        assert (
            matches_any_prefix(
                "github.com/anthropics/skills-evil/writing-style",
                ["github.com/anthropics/skills/*"],
            )
            is False
        )
