"""Scoped session_secrets containment end-to-end.

The wrapper subprocess path that this module used to exercise has
been retired (M1504 — the ``kiso.worker.wrapper`` module no longer
exists). Session-secret containment is still covered by the
unit-level tests in ``tests/test_tools.py`` and the sanitizer
redaction tests in ``tests/test_security.py``; no generic
replacement end-to-end runtime fixture exists yet.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration
