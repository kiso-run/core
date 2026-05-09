"""M1649 — `_ensure_image` lazy auto-build helper tests.

The multi-instance isolation test (M1601) currently SKIPS when
the local `kiso:latest` image is missing. That's a silent
coverage gap on a load-bearing isolation property.

This module pins the contract for the new
`_ensure_image(tag) -> tuple[bool, str | None]` helper:

- If the image is already present → return (True, None) without
  invoking `docker build`.
- If the image is absent and Docker daemon is reachable → run
  `docker build -t <tag> <_REPO_ROOT>` and return its outcome.
- If the image is absent and Docker is unreachable → return
  (False, "Docker daemon unreachable") without attempting build.
- If `docker build` fails → return (False, "<stderr tail>") so the
  caller can surface a diagnostic skip reason.

Tests mock `subprocess.run` to avoid actually invoking Docker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.functional.test_multi_instance_isolation import _ensure_image


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


class TestImagePresent:
    """No build attempted when the image is already present."""

    def test_returns_true_without_build(self):
        # docker image inspect returns 0 → present
        with patch(
            "tests.functional.test_multi_instance_isolation.subprocess.run",
            side_effect=[_proc(returncode=0)],  # image inspect
        ) as run_mock:
            ok, reason = _ensure_image("kiso:latest")
        assert ok is True
        assert reason is None
        assert run_mock.call_count == 1, (
            "expected only the image-inspect probe; build must NOT "
            "be invoked when the image is already present"
        )


class TestImageAbsentDockerAvailable:
    """Image absent + Docker reachable → run docker build."""

    def test_build_runs_and_succeeds(self):
        with patch(
            "tests.functional.test_multi_instance_isolation.subprocess.run",
            side_effect=[
                _proc(returncode=1),  # image inspect: absent
                _proc(returncode=0),  # docker info: available
                _proc(returncode=0),  # docker build: success
            ],
        ) as run_mock:
            ok, reason = _ensure_image("kiso:latest")
        assert ok is True
        assert reason is None
        # The third call must be a docker build invocation
        build_argv = run_mock.call_args_list[2].args[0]
        assert build_argv[0] == "docker"
        assert build_argv[1] == "build"
        assert "-t" in build_argv
        assert "kiso:latest" in build_argv

    def test_build_failure_returns_stderr_tail(self):
        with patch(
            "tests.functional.test_multi_instance_isolation.subprocess.run",
            side_effect=[
                _proc(returncode=1),  # image inspect: absent
                _proc(returncode=0),  # docker info: available
                _proc(returncode=1, stderr="Step 5/10: ...failed: missing base image"),
            ],
        ):
            ok, reason = _ensure_image("kiso:latest")
        assert ok is False
        assert reason is not None
        assert "missing base image" in reason or "failed" in reason.lower()


class TestImageAbsentDockerUnavailable:
    """Image absent + Docker daemon unreachable → no build attempt."""

    def test_no_build_without_docker(self):
        with patch(
            "tests.functional.test_multi_instance_isolation.subprocess.run",
            side_effect=[
                _proc(returncode=1),  # image inspect: absent
                _proc(returncode=1, stderr="Cannot connect to daemon"),  # docker info
            ],
        ) as run_mock:
            ok, reason = _ensure_image("kiso:latest")
        assert ok is False
        assert reason is not None
        # Build must NOT be attempted when Docker is down
        for call in run_mock.call_args_list:
            argv = call.args[0]
            assert argv[0:2] != ["docker", "build"], (
                f"docker build was attempted with no Docker daemon; argv={argv}"
            )


class TestBuildTimeout:
    """A hung build must surface a skip reason, not crash the test."""

    def test_subprocess_timeout_returns_false(self):
        import subprocess as sp

        def _run(*a, **kw):
            # First two calls succeed (image absent + docker available),
            # third (build) times out.
            if _run.calls == 0:
                _run.calls = 1
                return _proc(returncode=1)
            if _run.calls == 1:
                _run.calls = 2
                return _proc(returncode=0)
            raise sp.TimeoutExpired(cmd=["docker", "build"], timeout=1)

        _run.calls = 0
        with patch(
            "tests.functional.test_multi_instance_isolation.subprocess.run",
            side_effect=_run,
        ):
            ok, reason = _ensure_image("kiso:latest")
        assert ok is False
        assert reason is not None
        assert "timeout" in reason.lower() or "timed out" in reason.lower()


class TestDockerCheckEvaluatedLazily:
    """The docker-availability check must happen at TEST RUN time,
    not at module-import time. M1649b: a transient docker-info
    hiccup at import time used to freeze the skip mark for the
    whole pytest session — so a test would skip with 'Docker
    daemon unreachable' even if the daemon recovered before the
    test actually ran.
    """

    def test_no_module_level_docker_call(self):
        """AST-walk the module to verify no top-level
        `pytest.mark.skipif(...)` call passes a `_docker_available()`
        invocation as its first arg. That's the import-time-evaluation
        bug we're guarding against. Strings / docstrings / comments
        that mention the pattern in prose are ignored — only call
        nodes count."""
        import ast
        import inspect

        from tests.functional import test_multi_instance_isolation as mod

        tree = ast.parse(inspect.getsource(mod))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match `pytest.mark.skipif(...)`
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "skipif"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "mark"
            ):
                continue
            # First positional arg is the condition
            if not node.args:
                continue
            cond = node.args[0]
            # Look for any Call node whose func name matches
            # `_docker_available` in the condition subtree
            for sub in ast.walk(cond):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_docker_available"
                ):
                    offenders.append(ast.unparse(node)[:200])
                    break
        assert not offenders, (
            "tests/functional/test_multi_instance_isolation.py must not "
            "pass `_docker_available()` to a top-level "
            "`pytest.mark.skipif`. A transient docker-info hiccup at "
            "import freezes the skip for the whole pytest session. "
            "Move the check into the test body / `_ensure_image` "
            "(invoked at test-run time). Offenders: " + str(offenders)
        )

    def test_ensure_image_includes_docker_diagnostic(self):
        """When `_ensure_image` returns False because Docker is
        unreachable, the reason string must include the actual
        docker-info stderr tail so the skip is debuggable
        (instead of a generic 'unreachable')."""
        with patch(
            "tests.functional.test_multi_instance_isolation.subprocess.run",
            side_effect=[
                _proc(returncode=1),  # image inspect: absent
                _proc(returncode=1, stderr="permission denied while trying to connect"),
            ],
        ):
            ok, reason = _ensure_image("kiso:latest")
        assert ok is False
        assert reason is not None
        assert "permission denied" in reason.lower(), (
            f"reason must surface the actual docker-info stderr; got: {reason!r}"
        )
