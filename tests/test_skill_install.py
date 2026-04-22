"""Tests for ``kiso.skill_install`` — URL-based Agent Skills install.

Business requirement: a user pastes a skill URL and ``kiso skill
install --from-url <url>`` drops a working Agent Skill into
``~/.kiso/skills/``. The resolver must parse every documented URL
shape into an explicit plan; the installer must fetch + validate
the skill and leave a ``.provenance.json`` next to it.

URL forms covered:

- ``https://github.com/<owner>/<repo>`` — whole repo as one skill
  (top-level ``SKILL.md``) or single-skill subdirectory under
  ``skills/``.
- ``https://github.com/<owner>/<repo>/tree/<ref>/<path>`` — a named
  subpath at a specific ref.
- Any URL whose path ends ``SKILL.md`` or ``skill.md`` — raw file.
- ``*.zip`` — downloaded and unpacked.
- ``https://agentskills.io/skills/<slug>`` — translated via the
  redirect resolver to the backing github URL.
- Bare local path — delegates to the existing ``kiso skill add``
  code path.

Tests never hit the network; HTTP / git / zip runners are injected
via callables so the resolver stays fully offline-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from kiso.skill_install import (
    ResolvedSkill,
    SkillInstallError,
    install_resolved,
    resolve_from_url,
    write_provenance,
)


_STANDARD_SKILL = """\
---
name: python-debug
description: Helps debug Python tracebacks.
---

Read the traceback, isolate the failing frame, propose a fix.

## Planner
Reproduce, isolate, fix, verify.
"""


# ---------------------------------------------------------------------------
# resolve_from_url — pure URL parsing
# ---------------------------------------------------------------------------


class TestResolveFromUrl:
    def test_github_repo_root(self):
        r = resolve_from_url("https://github.com/acme/python-debug")
        assert r.source_type == "github_repo"
        assert r.source_url == "https://github.com/acme/python-debug"
        assert r.clone_url == "https://github.com/acme/python-debug"
        assert r.ref is None
        assert r.subpath is None
        assert r.staging_name == "python-debug"

    def test_github_repo_with_trailing_slash(self):
        r = resolve_from_url("https://github.com/acme/python-debug/")
        assert r.source_type == "github_repo"
        assert r.staging_name == "python-debug"

    def test_github_repo_with_dot_git(self):
        r = resolve_from_url("https://github.com/acme/python-debug.git")
        assert r.source_type == "github_repo"
        assert r.staging_name == "python-debug"

    def test_github_tree_subpath(self):
        r = resolve_from_url(
            "https://github.com/acme/skill-kit/tree/main/skills/writing-style"
        )
        assert r.source_type == "github_subpath"
        assert r.clone_url == "https://github.com/acme/skill-kit"
        assert r.ref == "main"
        assert r.subpath == "skills/writing-style"
        assert r.staging_name == "writing-style"

    def test_github_tree_deep_ref(self):
        r = resolve_from_url(
            "https://github.com/acme/skill-kit/tree/v1.2.3/skills/x/y"
        )
        assert r.ref == "v1.2.3"
        assert r.subpath == "skills/x/y"
        assert r.staging_name == "y"

    def test_raw_skill_md(self):
        r = resolve_from_url(
            "https://raw.githubusercontent.com/acme/writing-style/main/SKILL.md"
        )
        assert r.source_type == "raw_md"
        assert r.staging_name == "writing-style"

    def test_raw_skill_md_case_insensitive(self):
        r = resolve_from_url("https://example.com/path/skill.md")
        assert r.source_type == "raw_md"
        # staging name comes from parent dir of the .md file
        assert r.staging_name == "path"

    def test_zip_url(self):
        r = resolve_from_url("https://example.com/releases/skill-kit-1.0.zip")
        assert r.source_type == "zip"
        assert r.staging_name == "skill-kit-1-0"

    def test_agentskills_io_slug(self):
        # agentskills.io resolution needs an HTTP call to follow the
        # redirect; we inject a fetcher so the test stays offline.
        def fake_redirect_resolver(url: str) -> str:
            assert url == "https://agentskills.io/skills/writing-style"
            return "https://github.com/agentskills/writing-style"

        r = resolve_from_url(
            "https://agentskills.io/skills/writing-style",
            agentskills_resolver=fake_redirect_resolver,
        )
        assert r.source_type == "github_repo"
        assert r.clone_url == "https://github.com/agentskills/writing-style"
        assert r.staging_name == "writing-style"

    def test_local_path_existing_dir(self, tmp_path):
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(_STANDARD_SKILL)
        r = resolve_from_url(str(skill_dir))
        assert r.source_type == "local_path"
        assert r.local_path == skill_dir

    def test_local_path_existing_md_file(self, tmp_path):
        f = tmp_path / "my-skill.md"
        f.write_text(_STANDARD_SKILL)
        r = resolve_from_url(str(f))
        assert r.source_type == "local_path"
        assert r.local_path == f

    def test_name_hint_overrides_staging_name(self):
        r = resolve_from_url(
            "https://github.com/acme/repo",
            name_hint="custom-name",
        )
        assert r.staging_name == "custom-name"

    def test_name_hint_is_sanitized(self):
        r = resolve_from_url(
            "https://github.com/acme/repo",
            name_hint="Bad Name!",
        )
        # Must conform to Agent Skills naming.
        assert r.staging_name == "bad-name"

    def test_empty_url_rejected(self):
        with pytest.raises(SkillInstallError):
            resolve_from_url("")

    def test_unsupported_url_rejected(self):
        with pytest.raises(SkillInstallError) as exc:
            resolve_from_url("ftp://unknown.host/thing")
        assert "Supported" in str(exc.value) or "supported" in str(exc.value)

    def test_github_repo_no_owner_rejected(self):
        with pytest.raises(SkillInstallError):
            resolve_from_url("https://github.com/")


# ---------------------------------------------------------------------------
# install_resolved — fetch + place into target dir
# ---------------------------------------------------------------------------


class TestInstallResolvedRawMd:
    def test_raw_md_fetch_and_place(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()

        def fake_fetch_text(url: str) -> str:
            assert url.endswith("SKILL.md")
            return _STANDARD_SKILL

        resolved = resolve_from_url(
            "https://raw.example.com/python-debug/SKILL.md"
        )
        installed = install_resolved(
            resolved,
            target_dir=target,
            http_fetcher=fake_fetch_text,
        )
        # The canonical install path is ~/.kiso/skills/<frontmatter-name>/SKILL.md
        assert installed == target / "python-debug" / "SKILL.md"
        assert (target / "python-debug" / "SKILL.md").read_text() == _STANDARD_SKILL

    def test_raw_md_bad_frontmatter_rejected(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()

        def fake_fetch_text(url: str) -> str:
            return "not a skill\n"

        resolved = resolve_from_url("https://ex.com/x/SKILL.md")
        with pytest.raises(SkillInstallError):
            install_resolved(
                resolved, target_dir=target, http_fetcher=fake_fetch_text
            )
        assert not any(target.iterdir())

    def test_raw_md_refuses_overwrite_without_force(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()
        existing = target / "python-debug"
        existing.mkdir()
        (existing / "SKILL.md").write_text("# old")

        def fake_fetch_text(url: str) -> str:
            return _STANDARD_SKILL

        resolved = resolve_from_url("https://ex.com/x/SKILL.md")
        with pytest.raises(SkillInstallError):
            install_resolved(
                resolved,
                target_dir=target,
                http_fetcher=fake_fetch_text,
                force=False,
            )

    def test_raw_md_force_overwrites(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()
        existing = target / "python-debug"
        existing.mkdir()
        (existing / "SKILL.md").write_text("# old\n")

        def fake_fetch_text(url: str) -> str:
            return _STANDARD_SKILL

        resolved = resolve_from_url("https://ex.com/x/SKILL.md")
        install_resolved(
            resolved, target_dir=target, http_fetcher=fake_fetch_text, force=True
        )
        assert (target / "python-debug" / "SKILL.md").read_text() == _STANDARD_SKILL


class TestInstallResolvedGithub:
    def test_github_repo_top_level_skill_md(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()

        def fake_git_cloner(url: str, dest: Path, ref: str | None = None) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(_STANDARD_SKILL)
            (dest / "README.md").write_text("# info\n")

        resolved = resolve_from_url("https://github.com/acme/python-debug")
        installed = install_resolved(
            resolved, target_dir=target, git_cloner=fake_git_cloner
        )
        assert installed == target / "python-debug" / "SKILL.md"
        assert (target / "python-debug" / "SKILL.md").exists()
        # Sibling files come along when the repo IS the skill.
        assert (target / "python-debug" / "README.md").exists()

    def test_github_tree_subpath_install(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()

        def fake_git_cloner(url: str, dest: Path, ref: str | None = None) -> None:
            assert ref == "main"
            # Populate a multi-skill repo structure.
            sub = dest / "skills" / "writing-style"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "SKILL.md").write_text(
                _STANDARD_SKILL.replace("python-debug", "writing-style")
            )

        resolved = resolve_from_url(
            "https://github.com/acme/skill-kit/tree/main/skills/writing-style"
        )
        installed = install_resolved(
            resolved, target_dir=target, git_cloner=fake_git_cloner
        )
        assert installed == target / "writing-style" / "SKILL.md"

    def test_github_repo_no_skill_md_rejected(self, tmp_path):
        target = tmp_path / "skills"
        target.mkdir()

        def fake_git_cloner(url: str, dest: Path, ref: str | None = None) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "README.md").write_text("# not a skill\n")

        resolved = resolve_from_url("https://github.com/acme/plain-repo")
        with pytest.raises(SkillInstallError):
            install_resolved(
                resolved, target_dir=target, git_cloner=fake_git_cloner
            )


class TestInstallResolvedLocal:
    def test_local_path_directory(self, tmp_path):
        src = tmp_path / "src-skill"
        src.mkdir()
        (src / "SKILL.md").write_text(_STANDARD_SKILL)

        target = tmp_path / "skills"
        target.mkdir()

        resolved = resolve_from_url(str(src))
        installed = install_resolved(resolved, target_dir=target)
        assert installed == target / "python-debug" / "SKILL.md"


class TestWriteProvenance:
    def test_provenance_written_for_github_install(self, tmp_path):
        skill_dir = tmp_path / "python-debug"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(_STANDARD_SKILL)

        resolved = resolve_from_url("https://github.com/acme/python-debug")
        write_provenance(skill_dir, resolved)

        import json
        data = json.loads((skill_dir / ".provenance.json").read_text())
        assert data["source_url"] == "https://github.com/acme/python-debug"
        assert data["source_type"] == "github_repo"
        assert "installed_at" in data
