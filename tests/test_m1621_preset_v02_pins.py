"""M1621 — Default preset MCP plugin pins at v0.2.0.

Three plugins shipped v0.2.0 (May 2026) with pluggable local-first
backends and substantive feature additions:

- ``search-mcp`` v0.2.0 — adds ``deep_research`` (Sonar Pro), changes
  default ``web_search`` model to Sonar (cheaper fast tier), pluggable
  ``openrouter``/``litellm`` backend via ``KISO_SEARCH_BACKEND``.
- ``ocr-mcp`` v0.2.0 — pluggable ``tesseract`` (new default, local) /
  ``gemini`` (opt-in) backends. ``describe_image`` is Gemini-only.
- ``transcriber-mcp`` v0.2.0 — pluggable ``whisper-cpp`` (new default,
  local) / ``gemini`` (opt-in) backends.

The default preset must pin each at ``@v0.2.0`` so a fresh
``kiso init --preset default`` install gets the local-first /
privacy-first defaults aligned with B2B EU consumer expectations.
``aider-mcp`` is already at v0.2.0 — re-asserted here to lock the
shape against accidental rollback.
"""

from __future__ import annotations

import re

from kiso.mcp_presets import load_mcp_preset


_VERSION_RE = re.compile(r"git\+https://github\.com/kiso-run/[^@]+@v(?P<ver>[0-9.]+)")


def _kiso_run_pin(server: dict) -> str:
    """Return the kiso-run @vX.Y.Z pin from a server's args, or ''."""
    args = server.get("args") or []
    for a in args:
        m = _VERSION_RE.search(str(a))
        if m:
            return m.group("ver")
    return ""


class TestDefaultPresetV020Pins:
    """The 4 kiso-run plugins in the default preset must all pin
    @v0.2.0 — three are bumped in M1621, aider is already there."""

    def test_search_pinned_at_v020(self):
        servers = load_mcp_preset("default")["mcpServers"]
        pin = _kiso_run_pin(servers["search"])
        assert pin == "0.2.0", (
            f"search-mcp pin is {pin!r}; M1621 requires v0.2.0 to "
            f"ship the deep_research tool + pluggable backend default"
        )

    def test_ocr_pinned_at_v020(self):
        servers = load_mcp_preset("default")["mcpServers"]
        pin = _kiso_run_pin(servers["ocr"])
        assert pin == "0.2.0", (
            f"ocr-mcp pin is {pin!r}; M1621 requires v0.2.0 to ship "
            f"the local-first tesseract default backend"
        )

    def test_transcriber_pinned_at_v020(self):
        servers = load_mcp_preset("default")["mcpServers"]
        pin = _kiso_run_pin(servers["transcriber"])
        assert pin == "0.2.0", (
            f"transcriber-mcp pin is {pin!r}; M1621 requires v0.2.0 "
            f"to ship the local-first whisper-cpp default backend"
        )

    def test_aider_pinned_at_v020(self):
        """aider-mcp was already v0.2.0 before M1621 — pin it here so
        a future bump to v0.3 is an explicit decision, not a stealth
        regression."""
        servers = load_mcp_preset("default")["mcpServers"]
        pin = _kiso_run_pin(servers["aider"])
        assert pin == "0.2.0", (
            f"aider-mcp pin is {pin!r}; expected v0.2.0"
        )

    def test_post_init_message_documents_local_first_deps(self, tmp_path, monkeypatch, capsys):
        """``kiso init --preset default`` must surface guidance for the
        new local-first system dependencies (tesseract, whisper-cli)
        AND the ``KISO_*_BACKEND=gemini`` opt-out env vars for users
        who can't or don't want to install the local backends.

        Without this, a fresh install fails opaquely on
        ``ocr-mcp`` / ``transcriber-mcp`` doctor calls — the user
        has no idea why or how to recover."""
        import argparse
        from cli.init import run_init_command

        kiso_dir = tmp_path / ".kiso"
        config_path = kiso_dir / "config.toml"
        monkeypatch.setattr("cli.init.CONFIG_PATH", config_path)

        rc = run_init_command(argparse.Namespace(preset="default", force=False))
        assert rc == 0
        out = capsys.readouterr().out

        # Local-first system dependencies surfaced.
        assert "tesseract" in out.lower(), (
            "post-init must mention tesseract (ocr-mcp v0.2 default)"
        )
        assert "whisper" in out.lower(), (
            "post-init must mention whisper (transcriber-mcp v0.2 default)"
        )

        # Opt-out env vars for users who want the v0.1 cloud backends.
        assert "KISO_OCR_BACKEND" in out
        assert "KISO_TRANSCRIBER_BACKEND" in out

    def test_no_v02_capable_plugin_lags_at_v01(self):
        """Cross-cutting invariant for the four plugins that have
        shipped v0.2.0: none of them may be pinned at v0.1.x in the
        default preset. ``docreader-mcp`` is intentionally NOT in
        this list — its v0.2.0 (if any) is out of scope for M1621."""
        v02_capable = {"search", "ocr", "transcriber", "aider"}
        servers = load_mcp_preset("default")["mcpServers"]
        v01_pins = []
        for name in v02_capable:
            pin = _kiso_run_pin(servers[name])
            if pin and pin.startswith("0.1"):
                v01_pins.append((name, pin))
        assert not v01_pins, (
            f"M1621: kiso-run plugins still pinned at v0.1.x: "
            f"{v01_pins}. Bump to v0.2.0."
        )
