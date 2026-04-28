"""M1576 — regression locks for phantom skipif elimination.

A "phantom skipif" is a `@pytest.mark.skipif` decorator whose
precondition is satisfied by no runner currently invoked by
`utils/run_tests.sh` (or by any GitHub Actions workflow). Such
tests advertise themselves as part of a tier but never actually
run — they are dead surfaces.

This module asserts:
- `tests/test_dockerfile_runtime_baseline.py` has been moved to
  `tests/docker/` (where `run_docker()` invokes them inside a
  KISO_TEST_IMAGE=1 container).
- `TestLiveSandboxedSpawnOwnership` (the root-only test in
  `tests/test_mcp_sandbox_uid.py`) is gone — the unit-tier
  argument plumbing tests in the same file already cover the
  contract; the kernel-level drop verification needs a
  privileged tier that does not exist.
- `docker-compose.test.yml` does not declare the `test-unit`
  or `test-live` services that no caller invokes.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


class TestDockerfileBaselineRelocated:
    """The dockerfile baseline tests are split into two files:
    - `tests/test_dockerfile_manifests.py`: file-content checks on
      Dockerfile sources, runs in unit tier (host has disk access).
    - `tests/docker/test_dockerfile_runtime_baseline.py`: runtime
      invariants gated by KISO_TEST_IMAGE=1, runs in docker tier
      (inside the test image).
    The legacy single file is gone; both new files exist."""

    def test_old_combined_file_is_gone(self):
        assert not (_REPO / "tests" / "test_dockerfile_runtime_baseline.py").exists(), (
            "tests/test_dockerfile_runtime_baseline.py must be split "
            "into two files (manifests in unit tier, runtime invariants "
            "in docker tier)"
        )

    def test_manifest_file_exists_in_unit_tier(self):
        path = _REPO / "tests" / "test_dockerfile_manifests.py"
        assert path.exists(), (
            "tests/test_dockerfile_manifests.py must exist (manifest "
            "checks for Dockerfile / Dockerfile.test sources, runs in "
            "unit tier on the host)"
        )

    def test_runtime_invariants_file_exists_in_docker_tier(self):
        path = _REPO / "tests" / "docker" / "test_dockerfile_runtime_baseline.py"
        assert path.exists(), (
            "tests/docker/test_dockerfile_runtime_baseline.py must "
            "exist (KISO_TEST_IMAGE=1 invariants, runs inside the "
            "test image via run_docker())"
        )


class TestRootSandboxTestDeleted:
    """The root-only `TestLiveSandboxedSpawnOwnership` class
    needs `os.geteuid() == 0` to run. No runner in run_tests.sh
    or in CI runs as root, so the test never executes. The unit
    tests in the same file cover the argument plumbing; the
    kernel-level drop verification requires a privileged tier
    that does not exist (Option C from M1576 design discussion
    was rejected).
    """

    def test_class_definition_gone(self):
        text = (_REPO / "tests" / "test_mcp_sandbox_uid.py").read_text()
        assert "class TestLiveSandboxedSpawnOwnership" not in text, (
            "TestLiveSandboxedSpawnOwnership must be deleted — "
            "the test never runs (no privileged tier exists)"
        )
        assert "test_spawned_mcp_process_runs_as_sandbox_uid" not in text, (
            "the root-only test method must be deleted with its class"
        )


class TestComposeUnusedServicesGone:
    """`docker-compose.test.yml` must not declare services that no
    caller invokes. `test-unit` and `test-live` are legacy profiles
    that `utils/run_tests.sh` does not call (only `test-docker`
    and `test-functional` are referenced)."""

    COMPOSE = _REPO / "docker-compose.test.yml"

    def test_no_test_unit_service(self):
        text = self.COMPOSE.read_text()
        assert "test-unit:" not in text, (
            "docker-compose.test.yml must not declare a test-unit "
            "service — no caller invokes it"
        )

    def test_no_test_live_service(self):
        text = self.COMPOSE.read_text()
        assert "test-live:" not in text, (
            "docker-compose.test.yml must not declare a test-live "
            "service — no caller invokes it"
        )

    def test_no_unit_profile(self):
        text = self.COMPOSE.read_text()
        # The 'unit' profile was the test-unit service's profile.
        # After deletion, no service should declare profile 'unit'.
        assert "profiles: [unit]" not in text, (
            "no service should declare profile 'unit' after "
            "test-unit deletion"
        )

    def test_no_live_profile(self):
        text = self.COMPOSE.read_text()
        assert "profiles: [live]" not in text, (
            "no service should declare profile 'live' after "
            "test-live deletion"
        )
