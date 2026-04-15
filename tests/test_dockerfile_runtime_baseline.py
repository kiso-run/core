"""Tests that the runtime base images install the binaries the
sysenv probe and the v0.10 default preset depend on, plus a
runtime invariant for the test image (M1371): when running INSIDE
``Dockerfile.test``, ``npx`` must actually be on ``PATH`` — the
manifest assertions are not enough by themselves, since a buggy
build that strips the binary would still pass the manifest test.

Specifically: `nodejs` and `npm` must be installed in both the
production runtime image (`Dockerfile`) and the test image
(`Dockerfile.test`), so that:

- `npx -y <package>` works for npm-distributed MCP servers
  (the dominant distribution channel for the official
  `@modelcontextprotocol/*` reference servers and for
  `@playwright/mcp`).
- `kiso/sysenv.py:22-53` reports node/npm/npx as available
  rather than missing.
- `tests/live/test_mcp_reference_servers.py` no longer
  skips on the grounds that `npx` is unavailable.

`uvx` does NOT need a separate install line because `uv`
is already a hard dependency of kiso (see the
`COPY --from=ghcr.io/astral-sh/uv:latest` line) and `uvx`
ships as part of `uv`.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERFILE_TEST = REPO_ROOT / "Dockerfile.test"

# The kiso test image does not COPY the Dockerfiles into the runtime
# (only `kiso/`, `cli/`, `tests/` are copied), so manifest tests
# cannot run inside the container. Skip them gracefully when the
# files are absent — the runtime invariants in
# TestRuntimeImageInvariants cover the in-image side anyway.
_skip_if_dockerfiles_unreachable = pytest.mark.skipif(
    not DOCKERFILE.is_file() or not DOCKERFILE_TEST.is_file(),
    reason="Dockerfile / Dockerfile.test not in this checkout — "
    "manifest tests only run on a full repo checkout, not inside "
    "the kiso test image",
)


def _apt_install_packages(dockerfile_text: str) -> set[str]:
    """Return the set of packages installed by any `apt-get install` RUN.

    Concatenates packages from every `apt-get install ...` invocation in
    the file. Strips flags (`-y`, `--no-install-recommends`, etc.) and
    whitespace-separated continuation lines.
    """
    packages: set[str] = set()
    pattern = re.compile(
        r"apt-get\s+install\s+(.*?)(?:&&|$)",
        re.DOTALL,
    )
    for match in pattern.finditer(dockerfile_text):
        chunk = match.group(1)
        chunk = chunk.replace("\\\n", " ")
        for token in chunk.split():
            if token.startswith("-"):
                continue
            if token in {"&&", "rm", "-rf"}:
                continue
            packages.add(token)
    return packages


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


class TestRuntimeImageInvariants:
    """When running INSIDE the kiso test image, certain binaries must
    actually exist on PATH. The presence is signalled by the
    ``KISO_TEST_IMAGE=1`` env var that ``Dockerfile.test`` sets.

    Outside the test image (developer host, CI without docker) these
    tests pass trivially — they only enforce the invariant when we
    are *certain* we are inside the image kiso ships. This is the
    point where M1367 manifest changes get verified end-to-end.
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
