"""Manifest-level tests for `Dockerfile` and `Dockerfile.test`.

These verify that the production runtime image and the test image
declare the binaries the sysenv probe and the v0.10 default preset
depend on. They run on the developer host (or in CI) by reading the
Dockerfile sources directly — they do NOT require the image to be
built.

Specifically: `nodejs` and `npm` must be installed in both images so
that `npx -y <package>` works for npm-distributed MCP servers (the
dominant distribution channel for `@modelcontextprotocol/*` reference
servers and for `@playwright/mcp`).

`uvx` does NOT need a separate install line because `uv` is already
a hard dependency of kiso (`COPY --from=ghcr.io/astral-sh/uv:latest`
in both Dockerfiles).

Companion: `tests/docker/test_dockerfile_runtime_baseline.py` runs
INSIDE the test image (KISO_TEST_IMAGE=1) and checks the binaries
actually land on PATH. M1576 split this from a single legacy file
so the two distinct concerns each run in their proper tier.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERFILE_TEST = REPO_ROOT / "Dockerfile.test"

# Allow the manifest tests to skip gracefully when the Dockerfiles
# are not in the checkout (e.g. when this test file is somehow
# vendored into a downstream package without the build infra).
_skip_if_dockerfiles_unreachable = pytest.mark.skipif(
    not DOCKERFILE.is_file() or not DOCKERFILE_TEST.is_file(),
    reason="Dockerfile / Dockerfile.test not in this checkout — "
    "manifest tests only run on a full repo checkout",
)


def _apt_install_packages(dockerfile_text: str) -> set[str]:
    """Extract the set of packages installed via `apt install` /
    `apt-get install` from a Dockerfile's text. Strips flags
    (``-y``, ``--no-install-recommends``, version pins) and joins
    line continuations."""
    # Join line continuations.
    text = re.sub(r"\\\n\s*", " ", dockerfile_text)
    pkgs: set[str] = set()
    for m in re.finditer(
        r"apt(?:-get)?\s+install\s+(.+?)(?:&&|$)", text, re.MULTILINE,
    ):
        for tok in m.group(1).split():
            if tok.startswith("-"):
                continue  # skip flags
            tok = tok.split("=", 1)[0]  # strip version pin
            if tok and not tok.startswith("$"):
                pkgs.add(tok)
    return pkgs


@pytest.fixture(scope="module")
def runtime_dockerfile_packages() -> set[str]:
    return _apt_install_packages(DOCKERFILE.read_text())


@pytest.fixture(scope="module")
def test_dockerfile_packages() -> set[str]:
    return _apt_install_packages(DOCKERFILE_TEST.read_text())


@_skip_if_dockerfiles_unreachable
class TestRuntimeDockerfileNodeBaseline:
    """Production runtime image must ship Node.js + npm."""

    def test_dockerfile_exists(self) -> None:
        assert DOCKERFILE.is_file()

    def test_runtime_image_installs_nodejs(
        self, runtime_dockerfile_packages: set[str]
    ) -> None:
        assert "nodejs" in runtime_dockerfile_packages, (
            "Dockerfile must apt-install `nodejs` so MCP servers "
            "distributed via `npx` can run in the runtime image."
        )

    def test_runtime_image_installs_npm(
        self, runtime_dockerfile_packages: set[str]
    ) -> None:
        assert "npm" in runtime_dockerfile_packages, (
            "Dockerfile must apt-install `npm` so `npx` is on PATH "
            "(npx ships inside the npm package on Debian)."
        )


@_skip_if_dockerfiles_unreachable
class TestTestDockerfileNodeBaseline:
    """Test image must ship Node.js + npm so live MCP suites do not skip."""

    def test_dockerfile_test_exists(self) -> None:
        assert DOCKERFILE_TEST.is_file()

    def test_test_image_installs_nodejs(
        self, test_dockerfile_packages: set[str]
    ) -> None:
        assert "nodejs" in test_dockerfile_packages, (
            "Dockerfile.test must apt-install `nodejs` so the live MCP "
            "reference suite can exercise real npx-based servers."
        )

    def test_test_image_installs_npm(
        self, test_dockerfile_packages: set[str]
    ) -> None:
        assert "npm" in test_dockerfile_packages, (
            "Dockerfile.test must apt-install `npm` so `npx` is on PATH."
        )

    def test_test_image_sets_kiso_test_image_marker(self) -> None:
        """Dockerfile.test must set ``KISO_TEST_IMAGE=1``.

        The marker enables image-aware invariants (M1371): tests
        like ``tests/live/test_mcp_reference_servers.py`` use it to
        upgrade missing-binary skips into hard failures, so we
        catch M1367 regressions inside the build instead of years
        later.
        """
        text = DOCKERFILE_TEST.read_text()
        assert "KISO_TEST_IMAGE=1" in text, (
            "Dockerfile.test must set `ENV KISO_TEST_IMAGE=1` so "
            "image-aware tests can detect they are running inside "
            "the kiso test image."
        )


@_skip_if_dockerfiles_unreachable
class TestUvxAlreadyGratis:
    """Sanity: uv is already in both images, so uvx is gratis.

    This test does not require any new install line — it documents the
    invariant that justifies why we only add `nodejs npm` and not also
    a separate uv layer.
    """

    def test_runtime_image_pulls_uv(self) -> None:
        assert "ghcr.io/astral-sh/uv" in DOCKERFILE.read_text(), (
            "uv must be present in the runtime image; uvx ships with it."
        )

    def test_test_image_pulls_uv(self) -> None:
        assert "ghcr.io/astral-sh/uv" in DOCKERFILE_TEST.read_text(), (
            "uv must be present in the test image; uvx ships with it."
        )
