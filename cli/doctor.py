"""``kiso doctor`` — unified health-check driver.

One command covering every subsystem a Kiso instance depends on:
runtime binaries on ``PATH``, ``config.toml`` shape and credentials,
LLM provider reachability, the MCP pool, installed skills, sandbox
posture, the trust store, the SQLite session DB, and the workspace
directory. Each check returns one or more :class:`CheckResult`
rows which the driver aggregates. The CLI renders the rows as a
Rich table grouped by category (or JSON via ``--json``) and exits
0 only when every row is green; any ``fail`` row trips a non-zero
exit code so CI can gate deploys on ``kiso doctor``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

Status = Literal["ok", "warn", "fail"]

_VALID_STATUSES: tuple[str, ...] = ("ok", "warn", "fail")

_CATEGORIES: tuple[str, ...] = (
    "Runtime", "Config", "LLM", "MCP", "Skills",
    "Sandbox", "Trust", "Store", "Workspace", "Broker",
)


# M1592: heuristic keyword list for "the user requested a capability".
# Word-boundary-aware match; the broker check uses these to spot plans
# that ran exec when the briefer reported empty MCP catalog AND the
# user message hinted at a capability (transcribe / search / OCR / ...).
# Generalist by design — no MCP-specific names. New verbs are PRs, not
# auto-discovery, so the heuristic stays auditable.
CAPABILITY_INTENT_KEYWORDS: frozenset[str] = frozenset({
    "transcribe", "transcribing",
    "search", "searching", "find",
    "screenshot",
    "ocr", "extract",
    "summarize", "summary", "summarise",
    "translate", "translation",
    "render",
    "compile",
    "lint",
    "deploy",
    "fetch", "scrape", "crawl",
    "convert",
    "compress", "zip",
    "encode", "decode",
    "browse", "navigate",
})

_RUNTIME_TOOLS: tuple[tuple[str, Status, str], ...] = (
    ("uv", "fail", "Install with `curl -LsSf https://astral.sh/uv/install.sh | sh`"),
    ("uvx", "fail", "Ships with `uv`; install `uv` to get `uvx` on PATH"),
    ("npx", "warn", "Install Node.js (https://nodejs.org) to enable Node-based MCP servers"),
    ("git", "fail", "Install with your system package manager (e.g. `apt install git`)"),
)


@dataclass(frozen=True)
class CheckResult:
    """One row of the ``kiso doctor`` output.

    - ``category``: the section heading (``Runtime``, ``Config``, …)
    - ``name``: a short, stable identifier for the check
    - ``status``: ``ok`` | ``warn`` | ``fail``
    - ``detail``: free-form human-readable context
    - ``suggestion``: targeted remediation string (only for non-ok)
    """

    category: str
    name: str
    status: Status
    detail: str = ""
    suggestion: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"CheckResult.status must be one of {_VALID_STATUSES}, got {self.status!r}"
            )


@dataclass
class DoctorContext:
    """Shared context piped through every check.

    ``config`` may be ``None`` when we could not load a config (the
    Config check handles that explicitly); downstream checks guard
    against that case.
    """

    kiso_dir: Path
    config: Any | None  # kiso.config.Config — avoid the import cycle here
    config_path: Path
    api_key: str = ""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_checks(ctx: DoctorContext) -> list[CheckResult]:
    """Run every category and return the flat aggregate."""
    out: list[CheckResult] = []
    out.extend(check_runtime(ctx))
    out.extend(check_config(ctx))
    out.extend(check_llm(ctx))
    out.extend(check_mcp(ctx))
    out.extend(check_skills(ctx))
    out.extend(check_sandbox(ctx))
    out.extend(check_trust(ctx))
    out.extend(check_store(ctx))
    out.extend(check_workspace(ctx))
    out.extend(check_broker_invariants(ctx))
    return out


def exit_code_for_results(results: list[CheckResult]) -> int:
    """Return 0 when every row is ok (or warn), non-zero on any fail."""
    return 1 if any(r.status == "fail" for r in results) else 0


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def check_runtime(ctx: DoctorContext) -> list[CheckResult]:
    out: list[CheckResult] = []
    for tool, missing_status, suggestion in _RUNTIME_TOOLS:
        path = shutil.which(tool)
        if path:
            out.append(CheckResult(
                category="Runtime", name=tool, status="ok",
                detail=path,
            ))
        else:
            out.append(CheckResult(
                category="Runtime", name=tool, status=missing_status,
                detail=f"{tool} not found on PATH",
                suggestion=suggestion,
            ))

    # Python version — Kiso requires 3.12+ per pyproject.
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info >= (3, 12):
        out.append(CheckResult(
            category="Runtime", name="python", status="ok",
            detail=f"Python {pyver}",
        ))
    else:
        out.append(CheckResult(
            category="Runtime", name="python", status="fail",
            detail=f"Python {pyver} (3.12+ required)",
            suggestion="Install Python 3.12 or newer before running kiso",
        ))

    # KISO_DIR writable — later checks need somewhere to land.
    out.append(_check_dir_writable("kiso_dir_writable", ctx.kiso_dir))
    return out


def _check_dir_writable(name: str, path: Path) -> CheckResult:
    try:
        if not path.exists():
            return CheckResult(
                category="Runtime", name=name, status="fail",
                detail=f"{path} does not exist",
                suggestion=f"Create the directory: mkdir -p {path}",
            )
        probe = path / ".kiso-doctor-probe"
        probe.write_text("probe")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            category="Runtime", name=name, status="fail",
            detail=f"{path} not writable: {exc}",
            suggestion=f"Grant write access to {path}",
        )
    return CheckResult(
        category="Runtime", name=name, status="ok",
        detail=str(path),
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def check_config(ctx: DoctorContext) -> list[CheckResult]:
    out: list[CheckResult] = []
    if ctx.config_path.exists():
        try:
            text = ctx.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            out.append(CheckResult(
                category="Config", name="config_file", status="fail",
                detail=f"cannot read {ctx.config_path}: {exc}",
                suggestion=f"Check permissions on {ctx.config_path}",
            ))
        else:
            parse_error = _try_parse_toml(text)
            if parse_error is None:
                out.append(CheckResult(
                    category="Config", name="config_file", status="ok",
                    detail=str(ctx.config_path),
                ))
            else:
                out.append(CheckResult(
                    category="Config", name="config_file", status="fail",
                    detail=f"TOML parse error: {parse_error}",
                    suggestion=f"Fix syntax in {ctx.config_path}",
                ))
    else:
        out.append(CheckResult(
            category="Config", name="config_file", status="fail",
            detail=f"{ctx.config_path} does not exist",
            suggestion="Run `kiso init` to write a starter config.toml",
        ))

    if ctx.api_key:
        out.append(CheckResult(
            category="Config", name="openrouter_api_key", status="ok",
            detail="OPENROUTER_API_KEY is set",
        ))
    else:
        out.append(CheckResult(
            category="Config", name="openrouter_api_key", status="fail",
            detail="OPENROUTER_API_KEY is empty",
            suggestion=(
                "Export OPENROUTER_API_KEY in your shell or set it via "
                "`kiso env set OPENROUTER_API_KEY <value>`"
            ),
        ))
    return out


def _try_parse_toml(text: str) -> str | None:
    try:
        import tomllib
    except Exception:  # pragma: no cover — Python 3.11+ ships tomllib
        return None
    try:
        tomllib.loads(text)
    except Exception as exc:  # noqa: BLE001 — any parse failure is informational
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def check_llm(ctx: DoctorContext) -> list[CheckResult]:
    if not ctx.api_key:
        return [CheckResult(
            category="LLM", name="openrouter_reachable", status="warn",
            detail="skipped — no OPENROUTER_API_KEY",
            suggestion="Set OPENROUTER_API_KEY so the probe can authenticate",
        )]
    reachable, detail = _probe_openrouter(ctx)
    if reachable:
        return [CheckResult(
            category="LLM", name="openrouter_reachable", status="ok",
            detail=detail,
        )]
    return [CheckResult(
        category="LLM", name="openrouter_reachable", status="fail",
        detail=detail,
        suggestion=(
            "Check network egress and OPENROUTER_API_KEY validity; "
            "try `curl -H 'Authorization: Bearer $OPENROUTER_API_KEY' "
            "https://openrouter.ai/api/v1/models`"
        ),
    )]


def _probe_openrouter(ctx: DoctorContext) -> tuple[bool, str]:
    """Cheap GET against the provider's ``/models`` endpoint.

    Uses the configured ``openrouter`` provider when available so the
    probe hits the same base URL the runtime uses.
    """
    base_url = "https://openrouter.ai/api/v1"
    if ctx.config is not None:
        providers = getattr(ctx.config, "providers", None) or {}
        prov = providers.get("openrouter") if isinstance(providers, dict) else None
        if prov is not None and getattr(prov, "base_url", ""):
            base_url = prov.base_url.rstrip("/")
    url = base_url.rstrip("/") + "/models"
    try:
        import httpx
    except ImportError:
        return False, "httpx not installed"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {ctx.api_key}"},
            timeout=5.0,
        )
    except (httpx.HTTPError, socket.error) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if resp.status_code == 200:
        return True, f"{url} → 200 OK"
    return False, f"{url} → HTTP {resp.status_code}: {resp.text[:120]}"


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def check_mcp(ctx: DoctorContext) -> list[CheckResult]:
    servers: dict = {}
    if ctx.config is not None:
        servers = getattr(ctx.config, "mcp_servers", None) or {}
    if not servers:
        return [CheckResult(
            category="MCP", name="servers", status="ok",
            detail="no MCP servers configured",
        )]

    out: list[CheckResult] = []
    for name, server in servers.items():
        if not getattr(server, "enabled", True):
            out.append(CheckResult(
                category="MCP", name=f"mcp[{name}]", status="warn",
                detail=f"{name}: disabled in config",
            ))
            continue
        ok, detail = _probe_mcp_server(server)
        out.append(CheckResult(
            category="MCP",
            name=f"mcp[{name}]",
            status="ok" if ok else "fail",
            detail=detail,
            suggestion=(
                ""
                if ok
                else f"Inspect `kiso mcp logs {name}` and re-run `kiso mcp test {name}`"
            ),
        ))
    return out


def _probe_mcp_server(server: Any) -> tuple[bool, str]:
    """Spawn the MCP client long enough to complete the initialize
    handshake and a single ``tools/list`` call."""
    try:
        from kiso.mcp.http import MCPStreamableHTTPClient
        from kiso.mcp.schemas import MCPError
        from kiso.mcp.stdio import MCPStdioClient
    except Exception as exc:  # pragma: no cover — defensive import guard
        return False, f"import error: {exc}"

    async def _run() -> tuple[bool, str]:
        if getattr(server, "transport", "") == "stdio":
            client = MCPStdioClient(server)
        elif getattr(server, "transport", "") == "http":
            client = MCPStreamableHTTPClient(server)
        else:
            return False, f"unknown transport: {getattr(server, 'transport', '?')!r}"
        try:
            info = await client.initialize()
            methods = await client.list_methods()
            detail = (
                f"{info.name} v{info.version} — {len(methods)} method(s)"
            )
            return True, detail
        except MCPError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await client.shutdown()
            except Exception:  # noqa: BLE001
                pass

    try:
        return asyncio.run(_run())
    except RuntimeError:
        # Already inside an event loop (e.g. called from a test). Fall
        # back to a dedicated loop so the probe stays synchronous from
        # the caller's perspective.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def check_skills(ctx: DoctorContext) -> list[CheckResult]:
    skills_dir = ctx.kiso_dir / "skills"
    if not skills_dir.exists():
        return [CheckResult(
            category="Skills", name="skills_dir", status="ok",
            detail="no skills directory (no skills installed)",
        )]

    out: list[CheckResult] = []
    try:
        from kiso.skill_loader import discover_skills
    except Exception as exc:  # pragma: no cover — defensive
        return [CheckResult(
            category="Skills", name="skills_loader", status="fail",
            detail=f"import failed: {exc}",
            suggestion="Check that the kiso package is installed correctly",
        )]

    # Record discovery errors per-slot so malformed SKILL.md files surface.
    for entry in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            out.append(CheckResult(
                category="Skills", name=f"skill[{entry.name}]", status="warn",
                detail=f"{entry.name}: SKILL.md missing",
                suggestion=(
                    f"Reinstall the skill or delete the empty directory: "
                    f"rm -rf {entry}"
                ),
            ))
            continue
        try:
            skills = discover_skills(skills_dir)
            found = next(
                (s for s in skills if getattr(s, "name", "") == entry.name),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            out.append(CheckResult(
                category="Skills", name=f"skill[{entry.name}]", status="fail",
                detail=f"{entry.name}: discovery failed: {exc}",
                suggestion=(
                    f"Inspect {skill_md} — the frontmatter likely has a "
                    f"parse error"
                ),
            ))
            continue
        if found is None:
            out.append(CheckResult(
                category="Skills", name=f"skill[{entry.name}]", status="warn",
                detail=f"{entry.name}: SKILL.md present but not discovered",
                suggestion=(
                    f"Validate the frontmatter in {skill_md} — required "
                    f"fields: name, description"
                ),
            ))
        else:
            out.append(CheckResult(
                category="Skills", name=f"skill[{entry.name}]", status="ok",
                detail=f"{entry.name}",
            ))
    if not out:
        out.append(CheckResult(
            category="Skills", name="skills_dir", status="ok",
            detail="no skills installed",
        ))
    return out


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def check_sandbox(ctx: DoctorContext) -> list[CheckResult]:
    if os.geteuid() != 0:
        return [CheckResult(
            category="Sandbox", name="euid", status="ok",
            detail="running as non-root — per-session UID drop disabled",
        )]
    # Running as root: verify useradd is available.
    useradd = shutil.which("useradd")
    if useradd:
        return [CheckResult(
            category="Sandbox", name="useradd", status="ok",
            detail=useradd,
        )]
    return [CheckResult(
        category="Sandbox", name="useradd", status="fail",
        detail="running as root but `useradd` not found on PATH",
        suggestion=(
            "Install the system account utilities "
            "(e.g. `apt install passwd` or `yum install shadow-utils`) "
            "or run kiso as a non-root user"
        ),
    )]


# ---------------------------------------------------------------------------
# Trust store
# ---------------------------------------------------------------------------


def check_trust(ctx: DoctorContext) -> list[CheckResult]:
    path = ctx.kiso_dir / "trust.json"
    if not path.exists():
        return [CheckResult(
            category="Trust", name="trust_file", status="ok",
            detail="no custom trust.json — Tier 1 defaults apply",
        )]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [CheckResult(
            category="Trust", name="trust_file", status="fail",
            detail=f"cannot read {path}: {exc}",
            suggestion=f"Check permissions on {path}",
        )]
    try:
        json.loads(text)
    except ValueError as exc:
        return [CheckResult(
            category="Trust", name="trust_file", status="fail",
            detail=f"invalid JSON: {exc}",
            suggestion=(
                f"Fix {path} so it parses as JSON "
                f"(or delete it to restore Tier 1 defaults)"
            ),
        )]
    return [CheckResult(
        category="Trust", name="trust_file", status="ok",
        detail=str(path),
    )]


# ---------------------------------------------------------------------------
# Store (SQLite)
# ---------------------------------------------------------------------------


def check_store(ctx: DoctorContext) -> list[CheckResult]:
    out: list[CheckResult] = []
    db_path = ctx.kiso_dir / "kiso.db"
    if not db_path.exists():
        out.append(CheckResult(
            category="Store", name="db_file", status="ok",
            detail="no kiso.db yet — will be created on first run",
        ))
        return out
    out.append(CheckResult(
        category="Store", name="db_file", status="ok",
        detail=str(db_path),
    ))
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        out.append(CheckResult(
            category="Store", name="wal_mode", status="fail",
            detail=f"cannot open {db_path}: {exc}",
            suggestion=(
                f"Back up {db_path} and let kiso recreate it; "
                f"the existing file may be corrupt"
            ),
        ))
        return out
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        mode_str = mode[0] if mode else "<unknown>"
    finally:
        conn.close()
    if mode_str.lower() == "wal":
        out.append(CheckResult(
            category="Store", name="wal_mode", status="ok",
            detail="journal_mode=WAL",
        ))
    else:
        out.append(CheckResult(
            category="Store", name="wal_mode", status="warn",
            detail=f"journal_mode={mode_str}",
            suggestion=(
                f"Run `sqlite3 {db_path} 'PRAGMA journal_mode=WAL;'` "
                f"to enable write-ahead logging"
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Broker model invariants (M1592)
# ---------------------------------------------------------------------------


_CAPABILITY_INTENT_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(CAPABILITY_INTENT_KEYWORDS)) + r")\b",
    re.IGNORECASE,
)


def _has_capability_intent(content: str) -> bool:
    """True if the user message text triggers any heuristic verb."""
    if not content:
        return False
    return bool(_CAPABILITY_INTENT_RE.search(content))


def _broker_db_path(ctx: DoctorContext) -> Path:
    """The store DB path used by the broker invariant checks.

    `init_db` (kiso/store/setup.py) writes `store.db`; the legacy
    `check_store` looks at `kiso.db` instead. M1592 explicitly aligns
    with the live filename so the check runs against real plan rows.
    """
    return ctx.kiso_dir / "store.db"


def _fetch_recent_plans(db_path: Path, limit: int = 100) -> list[dict]:
    """Return up to *limit* most recent plans with the broker-relevant
    columns + each plan's task count by type. One round-trip pattern,
    no per-plan secondary query."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, session, goal, status, install_proposal, "
            "awaits_input, created_at FROM plans "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        plans = [dict(r) for r in cur.fetchall()]
        if not plans:
            return []
        ids = [p["id"] for p in plans]
        # SQLite parameter substitution does not handle a list directly.
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"SELECT plan_id, type, COUNT(*) AS n FROM tasks "
            f"WHERE plan_id IN ({placeholders}) GROUP BY plan_id, type",
            ids,
        )
        counts: dict[int, dict[str, int]] = {}
        for row in cur.fetchall():
            counts.setdefault(row["plan_id"], {})[row["type"]] = row["n"]
        for p in plans:
            p["task_counts"] = counts.get(p["id"], {})
    finally:
        conn.close()
    return plans


def _fetch_first_user_message(db_path: Path, session: str) -> str:
    """Return the most recent user message body for a session (or '')."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT content FROM messages WHERE session = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (session,),
        )
        row = cur.fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def check_broker_invariants(ctx: DoctorContext) -> list[CheckResult]:
    """M1592 broker-model invariant checks.

    Three drift signals against the last 100 plans:
    1. msg-only plans without any escape flag (validation breach).
    2. exec plans on capability-style intents (heuristic) — sign of
       prompt drift toward shell improv.
    3. `awaits_input=true` plans followed by an exec plan in the same
       session (silent self-resume bug).
    """
    out: list[CheckResult] = []
    db_path = _broker_db_path(ctx)
    if not db_path.exists():
        out.append(CheckResult(
            category="Broker", name="db_file", status="ok",
            detail=f"no {db_path.name} yet — broker invariants skipped",
        ))
        return out
    try:
        plans = _fetch_recent_plans(db_path, limit=100)
    except sqlite3.Error as exc:
        out.append(CheckResult(
            category="Broker", name="db_open", status="warn",
            detail=f"cannot open {db_path}: {exc}",
        ))
        return out
    if not plans:
        out.append(CheckResult(
            category="Broker", name="recent_plans", status="ok",
            detail="no plans in store yet",
        ))
        return out
    n = len(plans)

    # Check 1 — msg-only without any escape flag.
    bad_msg_only = 0
    for p in plans:
        counts = p.get("task_counts", {})
        non_msg = sum(v for k, v in counts.items() if k != "msg")
        if non_msg == 0 and counts:  # msg-only and at least one task
            has_escape = bool(
                p.get("install_proposal") or p.get("awaits_input")
            )
            if not has_escape:
                bad_msg_only += 1
    pct = (bad_msg_only / n) * 100
    if pct > 20:
        status: Status = "fail"
    elif pct > 5:
        status = "warn"
    else:
        status = "ok"
    out.append(CheckResult(
        category="Broker", name="msg_only_no_escape", status=status,
        detail=f"{bad_msg_only}/{n} plans ({pct:.1f}%) msg-only without escape flag",
        suggestion=(
            "Validation breaches that retry-passed via planner pivot — "
            "investigate planner.md drift if status is warn/fail"
            if status != "ok" else ""
        ),
    ))

    # Check 2 — exec on capability-style user message.
    exec_on_capability = 0
    for p in plans:
        counts = p.get("task_counts", {})
        if not counts.get("exec"):
            continue
        msg = _fetch_first_user_message(db_path, p["session"])
        if _has_capability_intent(msg):
            exec_on_capability += 1
    out.append(CheckResult(
        category="Broker", name="exec_on_capability_intent",
        status="warn" if exec_on_capability > 0 else "ok",
        detail=(
            f"{exec_on_capability}/{n} plans ran exec on a "
            f"capability-style request"
        ),
        suggestion=(
            "Capability requests should route through MCP or "
            "ask-first; exec is shell improv (decision 6)"
            if exec_on_capability > 0 else ""
        ),
    ))

    # Check 3 — awaits_input=true plan followed by an exec plan in
    # the same session (silent self-resume).
    by_session: dict[str, list[dict]] = {}
    for p in plans:
        by_session.setdefault(p["session"], []).append(p)
    silent_resume = 0
    for session_plans in by_session.values():
        # Plans are returned newest-first; iterate session timeline
        # oldest→newest.
        ordered = list(reversed(session_plans))
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.get("awaits_input") and (nxt.get("task_counts") or {}).get("exec"):
                silent_resume += 1
    out.append(CheckResult(
        category="Broker", name="awaits_input_self_resume",
        status="warn" if silent_resume > 0 else "ok",
        detail=(
            f"{silent_resume} awaits_input plans followed by exec "
            f"in the same session"
        ),
        suggestion=(
            "User reply should reclassify; silent exec after a "
            "broker pause is a bug (decision 1)"
            if silent_resume > 0 else ""
        ),
    ))
    return out


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


def check_workspace(ctx: DoctorContext) -> list[CheckResult]:
    out: list[CheckResult] = []
    for sub in ("sessions", "pub", "uploads"):
        target = ctx.kiso_dir / sub
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".kiso-doctor-probe"
            probe.write_text("probe")
            probe.unlink()
        except OSError as exc:
            out.append(CheckResult(
                category="Workspace", name=sub, status="fail",
                detail=f"{target} not writable: {exc}",
                suggestion=f"Grant write access to {target}",
            ))
            continue
        out.append(CheckResult(
            category="Workspace", name=sub, status="ok",
            detail=str(target),
        ))
    return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


_STATUS_GLYPH: dict[str, str] = {
    "ok": "OK",
    "warn": "WARN",
    "fail": "FAIL",
}


def render_json(results: list[CheckResult]) -> str:
    return json.dumps(
        [
            {
                "category": r.category,
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "suggestion": r.suggestion,
            }
            for r in results
        ],
        indent=2,
        sort_keys=False,
    )


def render_table(results: list[CheckResult]) -> str:
    """Return a plain-text table grouped by category.

    When ``rich`` is importable, the CLI uses it to render a styled
    table. This fallback is also what's asserted in tests — no hard
    dependency on a specific terminal width.
    """
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        return _render_plain(results)
    return _render_rich(results, Console, Table)


def _render_plain(results: list[CheckResult]) -> str:
    lines: list[str] = []
    for category in _CATEGORIES:
        rows = [r for r in results if r.category == category]
        if not rows:
            continue
        lines.append(f"## {category}")
        for r in rows:
            glyph = _STATUS_GLYPH[r.status]
            line = f"  {glyph:<4}  {r.name}"
            if r.detail:
                line += f"  — {r.detail}"
            lines.append(line)
            if r.status != "ok" and r.suggestion:
                lines.append(f"        ↳ {r.suggestion}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_rich(results: list[CheckResult], Console, Table) -> str:
    import io

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, no_color=True)
    for category in _CATEGORIES:
        rows = [r for r in results if r.category == category]
        if not rows:
            continue
        table = Table(
            title=category, show_header=True, header_style="bold",
            expand=False,
        )
        table.add_column("Status")
        table.add_column("Check")
        table.add_column("Detail")
        table.add_column("Suggestion")
        for r in rows:
            table.add_row(
                _STATUS_GLYPH[r.status], r.name, r.detail,
                r.suggestion if r.status != "ok" else "",
            )
        console.print(table)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run_doctor_command(args: Any) -> int:
    """CLI handler for ``kiso doctor``."""
    from kiso.config import CONFIG_PATH, KISO_DIR, LLM_API_KEY_ENV, reload_config

    ctx_config: Any | None
    try:
        ctx_config = reload_config(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 — broken config is itself a check
        log.debug("doctor: config load failed: %s", exc)
        ctx_config = None

    ctx = DoctorContext(
        kiso_dir=KISO_DIR,
        config=ctx_config,
        config_path=CONFIG_PATH,
        api_key=os.environ.get(LLM_API_KEY_ENV, "").strip(),
    )
    results = run_checks(ctx)
    if getattr(args, "json", False):
        sys.stdout.write(render_json(results) + "\n")
    else:
        sys.stdout.write(render_table(results))
    return exit_code_for_results(results)
