"""Runtime-image invariants — must run INSIDE the kiso test image.

When `Dockerfile.test` builds the test image, it sets
``KISO_TEST_IMAGE=1``. These tests assert that, given that marker,
certain binaries (`npx`, `uvx`) are actually reachable on PATH —
not just declared in the manifest.

Outside the test image (developer host, CI without docker) these
tests skip cleanly via the KISO_TEST_IMAGE check. Inside the test
image (i.e. when invoked by `run_docker()` via the `test-docker`
compose service) they fire and catch regressions where a build
ships an image without the binary the manifest promised.

Companion: `tests/test_dockerfile_manifests.py` checks the
Dockerfile *sources* (apt install lines, env declarations) on the
host where the source files are reachable. The two concerns are
distinct: manifests verify the build recipe; runtime invariants
verify the image actually delivers what the recipe promised.
"""

from __future__ import annotations

import os
import shutil

import pytest


class TestRuntimeImageInvariants:
    """When running INSIDE the kiso test image, certain binaries must
    actually exist on PATH. The presence is signalled by the
    ``KISO_TEST_IMAGE=1`` env var that ``Dockerfile.test`` sets.

    Outside the test image (developer host, CI without docker) these
    tests skip — they only enforce the invariant when we are
    *certain* we are inside the image kiso ships. This is the point
    where M1367 manifest changes get verified end-to-end.
    """

    @pytest.mark.skipif(
        os.environ.get("KISO_TEST_IMAGE") != "1",
        reason="not running inside the kiso test image (KISO_TEST_IMAGE!=1)",
    )
    def test_npx_present_in_test_image(self) -> None:
        assert shutil.which("npx") is not None, (
            "M1367 regression: KISO_TEST_IMAGE=1 but `npx` is not on PATH. "
            "Dockerfile.test must apt-install `nodejs npm` and the binary "
            "must be reachable from a non-login shell."
        )

    @pytest.mark.skipif(
        os.environ.get("KISO_TEST_IMAGE") != "1",
        reason="not running inside the kiso test image (KISO_TEST_IMAGE!=1)",
    )
    def test_uvx_present_in_test_image(self) -> None:
        assert shutil.which("uvx") is not None, (
            "uvx must be present in the kiso test image. uvx ships with "
            "uv, which is already a hard dependency of kiso."
        )
