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


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _image_present(tag: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


_skip_unless_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon unreachable — skipping multi-instance isolation test",
)
_skip_unless_image = pytest.mark.skipif(
    not _image_present(_KISO_IMAGE),
    reason=(
        f"Local `{_KISO_IMAGE}` image missing — build it via `docker build -t "
        f"{_KISO_IMAGE} .` to run this test"
    ),
)


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


@_skip_unless_docker
@_skip_unless_image
def test_two_instances_keep_sessions_isolated(tmp_path):
    """Sessions created in instance A do NOT appear in instance B's listing.

    This is the data-plane isolation contract for the multi-instance
    feature: separate `~/.kiso` volumes → separate `store.db` files →
    one instance's session table is invisible to the other.
    """
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
