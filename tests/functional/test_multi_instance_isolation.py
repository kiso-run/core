"""M1601 — Docker functional multi-instance isolation (closes M1586 layer Docker).

Two real `kiso` containers running side-by-side must keep their
session state isolated end-to-end. The unit-tier regex lock (M1586)
proves names validate; the BATS layer (M1599 via
`test_host_resolve_instance.bats`) proves the wrapper script
dispatches; this Docker layer proves the data-plane isolation.

The test spawns two containers backed by separate volume mounts,
POSTs a distinct session id to each, and asserts each instance's
`GET /sessions?all=true` lists only its own session. No LLM
involvement — session CRUD is pure DB-backed and exercises the
isolation property without relying on planner/messenger behaviour.

Skips cleanly when:
- Docker daemon is unreachable.
- The local `kiso` image (defaulting to ``kiso:latest``) is not built.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest


pytestmark = [pytest.mark.functional, pytest.mark.requires_docker]


_KISO_IMAGE = os.environ.get("KISO_TEST_IMAGE_TAG", "kiso:latest")
_HEALTH_TIMEOUT_S = 30.0
_HEALTH_POLL_INTERVAL_S = 0.5
_BUILD_TIMEOUT_S = 600.0  # 10 min for a clean-cache build; cache-hit ≪ 10s

# Repo root (the directory containing pyproject.toml + Dockerfile).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _docker_status() -> tuple[bool, str]:
    """Probe Docker daemon reachability.

    Returns ``(True, "")`` when reachable, ``(False, diagnostic)``
    otherwise. The diagnostic surfaces the actual blocker (binary
    missing / `docker info` stderr / timeout) so skipped runs are
    debuggable without re-running anything. Single subprocess
    call — used by both the boolean check and the skip-reason
    formatter, so we never double-probe.
    """
    if shutil.which("docker") is None:
        return False, "docker binary not on PATH"
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False, "docker info timed out after 5s"
    except OSError as e:
        return False, f"docker info OS error: {e}"
    if r.returncode == 0:
        return True, ""
    tail = (r.stderr or r.stdout or "(no output)").strip()
    return False, f"docker info exit={r.returncode}: {tail[-300:]}"


def _docker_available() -> bool:
    return _docker_status()[0]


def _image_present(tag: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _ensure_image(tag: str) -> tuple[bool, str | None]:
    """Ensure the local image *tag* exists, building it from the
    repo Dockerfile when missing.

    Returns ``(True, None)`` when the image is available (already
    present or built fresh), ``(False, reason)`` otherwise.

    Skip-reason strings are diagnostic — they surface the actual
    blocker (Docker daemon unreachable / build stderr / build
    timeout) so a CI run that skips this test is debuggable
    without re-running anything.

    Repeated builds hit Docker's layer cache; the cost is paid
    once per source change. M1649 motivation: the prior
    `pytest.mark.skipif(not _image_present(...))` silently skipped
    the test on every host that hadn't pre-built the image, which
    was a coverage gap on a load-bearing isolation property.
    """
    if _image_present(tag):
        return True, None
    docker_ok, docker_reason = _docker_status()
    if not docker_ok:
        return False, docker_reason or "Docker daemon unreachable"
    try:
        r = subprocess.run(
            ["docker", "build", "-t", tag, str(_REPO_ROOT)],
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker build timed out after {_BUILD_TIMEOUT_S}s"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "(no output)").strip()
        return False, tail[-1000:]
    return True, None


# M1649b: the previous `pytest.mark.skipif(not _docker_available(), …)`
# evaluated `_docker_available()` at MODULE-IMPORT time. A transient
# `docker info` hiccup at that exact moment (slow daemon, cold start,
# 5s timeout edge) froze the skip for the whole pytest session — even
# if the daemon recovered before the test would actually run. The
# docker availability check now happens at TEST-RUN time inside the
# test body via `_ensure_image`, which surfaces the actual stderr tail
# in the skip reason.


def _free_port() -> int:
    """Bind to an ephemeral port, return the number, release the socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_minimal_config(kiso_dir: Path, token: str) -> None:
    """Drop a minimal config.toml so the kiso server can boot.

    No real LLM provider is configured — this test only exercises
    session CRUD endpoints which run entirely on the local SQLite DB.
    """
    (kiso_dir / "config.toml").write_text(
        "\n".join([
            "[tokens]",
            f'cli = "{token}"',
            "",
            "[providers.openrouter]",
            'base_url = "https://openrouter.ai/api/v1"',
            "",
            "[users.testadmin]",
            'role = "admin"',
            "",
            "[settings]",
            "external_url = \"http://localhost\"",
            "",
        ]) + "\n",
        encoding="utf-8",
    )


@contextmanager
def _kiso_container(kiso_dir: Path, host_port: int, token: str):
    """Run a kiso container bound to host_port, yield the URL, then tear down."""
    name = f"kiso-test-{uuid.uuid4().hex[:8]}"
    _write_minimal_config(kiso_dir, token)
    cmd = [
        "docker", "run", "--rm", "-d",
        "--name", name,
        "-p", f"127.0.0.1:{host_port}:8333",
        "-v", f"{kiso_dir}:/root/.kiso",
        _KISO_IMAGE,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker run failed for {name}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        url = f"http://127.0.0.1:{host_port}"
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_err = ""
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{url}/health", timeout=2.0)
                if r.status_code == 200:
                    yield url
                    return
                last_err = f"HTTP {r.status_code}"
            except (httpx.HTTPError, OSError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        raise TimeoutError(
            f"{name} did not become healthy within {_HEALTH_TIMEOUT_S}s "
            f"(last error: {last_err})"
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, timeout=10,
        )


def test_two_instances_keep_sessions_isolated(tmp_path):
    """Sessions created in instance A do NOT appear in instance B's listing.

    This is the data-plane isolation contract for the multi-instance
    feature: separate `~/.kiso` volumes → separate `store.db` files →
    one instance's session table is invisible to the other.
    """
    # M1649: lazy-build the kiso image when missing (instead of
    # silently skipping). Build cost is paid once per source change;
    # subsequent runs hit the Docker layer cache.
    ok, reason = _ensure_image(_KISO_IMAGE)
    if not ok:
        pytest.skip(f"Cannot ensure `{_KISO_IMAGE}` image: {reason}")
    kiso_dir_a = tmp_path / "instance_a"
    kiso_dir_b = tmp_path / "instance_b"
    kiso_dir_a.mkdir()
    kiso_dir_b.mkdir()

    token = "mt-isolation-test-token"
    headers = {"Authorization": f"Bearer {token}"}
    session_a = f"alpha-{uuid.uuid4().hex[:8]}"
    session_b = f"beta-{uuid.uuid4().hex[:8]}"

    port_a = _free_port()
    port_b = _free_port()

    with (
        _kiso_container(kiso_dir_a, port_a, token) as url_a,
        _kiso_container(kiso_dir_b, port_b, token) as url_b,
    ):
        # Create one session per instance.
        for url, session in ((url_a, session_a), (url_b, session_b)):
            r = httpx.post(
                f"{url}/sessions",
                headers=headers,
                json={"session": session, "user": "testadmin"},
                timeout=5.0,
            )
            assert r.status_code in (200, 201), (
                f"POST /sessions to {url} returned {r.status_code}: {r.text}"
            )

        # Each instance's listing must only see its own session.
        params = {"user": "testadmin", "all": "true"}
        list_a = httpx.get(
            f"{url_a}/sessions", headers=headers, params=params, timeout=5.0,
        )
        list_b = httpx.get(
            f"{url_b}/sessions", headers=headers, params=params, timeout=5.0,
        )
        assert list_a.status_code == 200, f"GET /sessions A: {list_a.text}"
        assert list_b.status_code == 200, f"GET /sessions B: {list_b.text}"

        ids_a = _extract_session_ids(list_a.json())
        ids_b = _extract_session_ids(list_b.json())

        assert session_a in ids_a, f"A missing its own session {session_a}: {ids_a}"
        assert session_b in ids_b, f"B missing its own session {session_b}: {ids_b}"
        assert session_b not in ids_a, (
            f"isolation breach: A sees B's session {session_b!r} in {ids_a}"
        )
        assert session_a not in ids_b, (
            f"isolation breach: B sees A's session {session_a!r} in {ids_b}"
        )


def _extract_session_ids(payload) -> set[str]:
    """Pull session ids out of a /sessions response, tolerant to shape."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("sessions") or payload.get("items") or []
    else:
        rows = []
    ids: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            ids.add(row)
        elif isinstance(row, dict):
            sid = row.get("session") or row.get("id") or row.get("session_id")
            if sid:
                ids.add(str(sid))
    return ids
