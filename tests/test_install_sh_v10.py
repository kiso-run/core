"""Invariants on ``install.sh`` for the v0.10 single-key UX.

The installer must not carry stale v0.9 model roles in its fallback
table (the `searcher` role was retired) and must honor
``OPENROUTER_API_KEY`` as an env-var fallback when the user runs the
script non-interactively (e.g. ``curl … | sh``) without the
``--api-key`` flag.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"


def _source() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


class TestNoSearcherFallback:
    def test_searcher_not_in_model_fallback(self) -> None:
        # The ``ask_models`` FALLBACK heredoc must not list ``searcher``
        # (the role is retired in v0.10). The Python source of truth in
        # ``kiso/config.py`` is canonical; the fallback is used only when
        # ``python3 -c 'from kiso.config ...'`` fails.
        text = _source()
        assert "searcher|" not in text, (
            "install.sh FALLBACK heredoc still lists the retired `searcher` "
            "role. Remove the line."
        )


class TestOpenRouterEnvFallback:
    def test_env_var_fallback_in_api_key_path(self) -> None:
        # If no ``--api-key`` flag is passed, the installer should accept
        # the value of ``$OPENROUTER_API_KEY`` (when set in the process
        # environment) before falling back to the interactive prompt.
        # This is what makes the curl-pipe-sh quickstart work.
        text = _source()
        assert "OPENROUTER_API_KEY" in text, (
            "install.sh must reference OPENROUTER_API_KEY somewhere"
        )
        assert 'API_KEY="${OPENROUTER_API_KEY' in text or \
               'API_KEY=$OPENROUTER_API_KEY' in text or \
               'API_KEY="${OPENROUTER_API_KEY:-}"' in text or \
               '${OPENROUTER_API_KEY:-}' in text, (
            "install.sh ask_api_key must fall back to the OPENROUTER_API_KEY "
            "env var when --api-key is not provided (so curl | sh works with "
            "only the env var set)"
        )


class TestNoStaleKisoLlmApiKey:
    def test_kiso_llm_api_key_not_referenced(self) -> None:
        # M1521 unified on OPENROUTER_API_KEY; the old env name
        # KISO_LLM_API_KEY must not appear in the installer.
        text = _source()
        assert "KISO_LLM_API_KEY" not in text, (
            "install.sh still references the retired KISO_LLM_API_KEY env "
            "var. Use OPENROUTER_API_KEY."
        )
