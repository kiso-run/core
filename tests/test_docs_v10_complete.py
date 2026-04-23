"""v0.10 docs-completeness invariants.

Enforces the M1523b "Done when" contract: every file in ``docs/`` reflects
v0.10 architecture; no active usage of retired concepts (wrapper /
recipe task types, ``searcher`` role, "registry" of servers); a docs
index exists so a new user can navigate top-down; internal markdown
links resolve.

Historical references in past tense are allowed. Active usage strings
(task type in current lists, current roles table, "wrappers:" config
key, `kiso wrapper`/`kiso recipe` verbs) are not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _doc_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


class TestDocsIndex:
    def test_docs_index_exists(self) -> None:
        assert (DOCS / "index.md").is_file(), (
            "docs/index.md is the v0.10 docs entry point; a new user needs "
            "a top-level map. Create it linking the core docs in reading order."
        )

    def test_docs_index_links_core_docs(self) -> None:
        idx = (DOCS / "index.md").read_text(encoding="utf-8")
        for required in (
            "architecture.md",
            "flow.md",
            "skills.md",
            "mcp.md",
            "cli.md",
            "config.md",
            "security.md",
        ):
            assert required in idx, (
                f"docs/index.md must link {required} (docs tree entry point)"
            )


class TestNoCurrentTaskTypeRetired:
    """The planner emits four task types in v0.10: exec, mcp, msg, replan.

    Any doc that presents a current task-type list including ``wrapper``
    or ``search`` is teaching a retired contract.
    """

    TASK_TYPE_LIST_PATTERNS = (
        re.compile(r"exec\s*[/|,]\s*wrapper", re.IGNORECASE),
        re.compile(r"wrapper\s*[/|,]\s*search", re.IGNORECASE),
        re.compile(r"exec\s*\|\s*wrapper\s*\|", re.IGNORECASE),
        re.compile(r"wrapper\s*\|\s*search\s*\|", re.IGNORECASE),
    )

    @pytest.mark.parametrize(
        "pattern",
        [p.pattern for p in TASK_TYPE_LIST_PATTERNS],
    )
    def test_no_current_task_type_list_includes_retired(
        self, pattern: str
    ) -> None:
        rx = re.compile(pattern, re.IGNORECASE)
        offenders: list[str] = []
        for p in _doc_files():
            text = p.read_text(encoding="utf-8")
            if rx.search(text):
                offenders.append(p.relative_to(ROOT).as_posix())
        assert not offenders, (
            f"pattern {pattern!r} lists retired task types as current in: "
            f"{', '.join(offenders)}"
        )


class TestNoCurrentSearcherRole:
    """The ``searcher`` LLM role was retired with the built-in search
    task. Roles tables and model-config examples in docs must no longer
    list it as a current role.
    """

    def test_config_md_model_table_has_no_searcher(self) -> None:
        cfg = (DOCS / "config.md").read_text(encoding="utf-8")
        assert "searcher" not in cfg.lower(), (
            "docs/config.md still references the retired `searcher` role "
            "(model config / required roles list). v0.10 drops it."
        )

    def test_llm_roles_no_current_searcher_section(self) -> None:
        roles = (DOCS / "llm-roles.md")
        if not roles.is_file():
            pytest.skip("docs/llm-roles.md not present")
        text = roles.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "### searcher" not in lowered, (
            "docs/llm-roles.md still has a current `### Searcher` section"
        )
        assert "## searcher" not in lowered, (
            "docs/llm-roles.md still has a current `## Searcher` section"
        )


class TestNoCurrentWrapperConfigKey:
    """``users.<name>.wrappers`` is a retired v0.9 config key. v0.10
    uses per-user MCP / skill filters instead. No doc should advertise
    the old key as current config.
    """

    FORBIDDEN_CONFIG_KEYS = (
        "users.*.wrappers",
        'wrappers = "*"',
        'wrappers = ["search"',
    )

    @pytest.mark.parametrize("phrase", FORBIDDEN_CONFIG_KEYS)
    def test_config_key_retired(self, phrase: str) -> None:
        offenders: list[str] = []
        for p in _doc_files():
            if phrase in p.read_text(encoding="utf-8"):
                offenders.append(p.relative_to(ROOT).as_posix())
        assert not offenders, (
            f"retired config key {phrase!r} still in: "
            f"{', '.join(offenders)}"
        )


class TestConfigMdHasV10Settings:
    """config.md must document at least the flagship v0.10 settings."""

    REQUIRED = (
        "mcp_session_idle_timeout",
        "mcp_max_session_clients_per_server",
        "briefer_skill_filter_threshold",
    )

    @pytest.mark.parametrize("key", REQUIRED)
    def test_setting_documented(self, key: str) -> None:
        text = (DOCS / "config.md").read_text(encoding="utf-8")
        assert key in text, (
            f"docs/config.md must document v0.10 setting {key!r}"
        )


class TestInternalLinksResolve:
    """Every relative markdown link inside docs/ must point at a real file.

    Scans ``[text](path.md)`` and ``[text](path.md#anchor)`` where
    ``path`` is relative (no ``://``), does not start with ``/``,
    and looks like a doc reference. Absolute paths from the repo root
    referenced by other files count as valid only if they exist on disk.
    """

    LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    def _collect_links(self) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        for p in _doc_files():
            text = p.read_text(encoding="utf-8")
            for match in self.LINK_RE.finditer(text):
                target = match.group(1).strip()
                if not target:
                    continue
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if target.startswith("#"):
                    continue
                out.append((p, target))
        return out

    def test_no_broken_internal_links(self) -> None:
        broken: list[str] = []
        for source, target in self._collect_links():
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if path_part.startswith("/"):
                candidate = Path(path_part)
                if candidate.is_absolute() and not candidate.exists():
                    broken.append(
                        f"{source.relative_to(ROOT).as_posix()} -> {target}"
                    )
                continue
            resolved = (source.parent / path_part).resolve()
            if not resolved.exists():
                broken.append(
                    f"{source.relative_to(ROOT).as_posix()} -> {target}"
                )
        assert not broken, (
            "broken internal links in docs:\n  " + "\n  ".join(broken)
        )
