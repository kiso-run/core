"""Scoped session_secrets containment end-to-end (M1273).

Unit-level containment for ``build_wrapper_input`` is already covered in
``tests/test_tools.py:1092-1138`` (test_scoped_session_secrets,
test_no_declared_secrets_scoped_empty, test_none_session_secrets), and
``sanitize_output`` redaction is covered in
``tests/test_security.py:174-240``.

This module covers the **end-to-end runtime path**: the full flow from
session_secrets passed to ``_wrapper_task`` → real subprocess execution
→ tool stdout. The fake tool from M1268 echoes the stdin payload so
the test can assert exactly what the subprocess actually received.

The goal is to prove that:

- declared secrets reach the wrapper subprocess via stdin
- undeclared secrets do NOT reach the tool, neither via stdin nor as
  env vars (containment)
- the value of an undeclared secret never appears anywhere in the
  tool's view of the world
"""

from __future__ import annotations

import json

import pytest

from kiso.worker.wrapper import _wrapper_task

from tests.integration.conftest import fake_wrapper  # noqa: F401  (fixture)


pytestmark = pytest.mark.integration


class TestSessionSecretContainmentEndToEnd:

    async def test_only_declared_secret_reaches_subprocess(
        self, fake_wrapper, tmp_path,
    ):
        """The declared key DECLARED_KEY is passed; UNDECLARED_KEY is
        scoped out and never visible to the subprocess."""
        from unittest.mock import patch

        secrets = {
            "DECLARED_KEY": "declared-value",
            "UNDECLARED_KEY": "undeclared-secret-value",
        }

        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            stdout, stderr, success, rc = await _wrapper_task(
                session="secret-containment-1",
                wrapper=fake_wrapper,
                args={},
                plan_outputs=[],
                session_secrets=secrets,
            )

        assert success, f"tool subprocess failed: stderr={stderr!r}"
        report = json.loads(stdout)

        assert report["session_secrets_keys"] == ["DECLARED_KEY"]
        assert report["session_secrets_values"] == {"DECLARED_KEY": "declared-value"}
        assert "UNDECLARED_KEY" not in report["session_secrets_keys"]

    async def test_undeclared_secret_value_not_in_env(
        self, fake_wrapper, tmp_path,
    ):
        """The value of an undeclared secret must not appear in the
        tool subprocess env, even by accident."""
        from unittest.mock import patch

        secrets = {
            "DECLARED_KEY": "declared-value",
            "UNDECLARED_KEY": "absolutely-secret-token-xyz",
        }

        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            stdout, _stderr, success, _rc = await _wrapper_task(
                session="secret-containment-2",
                wrapper=fake_wrapper,
                args={},
                plan_outputs=[],
                session_secrets=secrets,
            )

        assert success
        report = json.loads(stdout)

        # The undeclared secret value must not appear anywhere
        env_keys = report["env_keys_visible"]
        assert not any("absolutely-secret-token-xyz" in k for k in env_keys)
        assert "absolutely-secret-token-xyz" not in stdout.replace(
            json.dumps(report["session_secrets_values"]), ""
        )

    async def test_none_session_secrets_yields_empty_scope(
        self, fake_wrapper, tmp_path,
    ):
        """Calling _wrapper_task with session_secrets=None yields an empty
        session_secrets dict in the wrapper stdin payload."""
        from unittest.mock import patch

        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            stdout, _stderr, success, _rc = await _wrapper_task(
                session="secret-containment-3",
                wrapper=fake_wrapper,
                args={},
                plan_outputs=[],
                session_secrets=None,
            )

        assert success
        report = json.loads(stdout)
        assert report["session_secrets_keys"] == []
        assert report["session_secrets_values"] == {}
