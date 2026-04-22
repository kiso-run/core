"""URL resolver and installer for ``kiso skill install --from-url``.

Given a user-supplied URL identifying an Agent Skill, produces a
normalised install plan (``ResolvedSkill``) and, when asked, executes
the fetch and writes the skill into ``~/.kiso/skills/<name>/``.

URL forms supported:

- ``https://github.com/<owner>/<repo>`` — git-clone the whole repo;
  install either the top-level ``SKILL.md`` (and sibling files) or a
  single-skill subdirectory under ``skills/``.
- ``https://github.com/<owner>/<repo>/tree/<ref>/<path>`` — clone at
  the specified ref and install the given subpath.
- Any URL whose path ends with ``SKILL.md`` or ``skill.md`` — raw
  single-file fetch.
- Any URL whose path ends with ``.zip`` — download and unpack.
- ``https://agentskills.io/skills/<slug>`` — resolved via the
  redirect (injected ``agentskills_resolver``) to a backing
  github URL; the resolver is then re-invoked.
- Bare local path to a directory with ``SKILL.md`` or a single
  ``.md`` file — delegates to the same copy path as
  ``kiso skill add``.

Network, git, and zip operations are wrapped behind injectable
callables so unit tests can run offline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

from kiso.config import SKILLS_DIR as _DEFAULT_SKILLS_DIR
from kiso.skill_loader import parse_skill_file


PROVENANCE_FILE = ".provenance.json"

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9-]+")


class SkillInstallError(Exception):
    """Raised when a URL cannot be resolved or the install fails."""


SkillSourceType = Literal[
    "github_repo", "github_subpath", "raw_md", "zip", "local_path"
]


@dataclass
class ResolvedSkill:
    """Normalised install plan for a single Agent Skill."""

    source_type: SkillSourceType
    source_url: str
    staging_name: str
    clone_url: str | None = None
    ref: str | None = None
    subpath: str | None = None
    local_path: Path | None = None


# ---------------------------------------------------------------------------
# resolve_from_url — pure URL parsing
# ---------------------------------------------------------------------------


def resolve_from_url(
    url: str,
    *,
    name_hint: str | None = None,
    agentskills_resolver: Callable[[str], str] | None = None,
) -> ResolvedSkill:
    """Parse *url* and return a :class:`ResolvedSkill` plan.

    Does NOT hit the network for github / raw / zip URLs — pure string
    parsing. ``agentskills.io`` URLs DO call ``agentskills_resolver``
    (which the caller wires to an HTTP redirect follower).

    Raises :class:`SkillInstallError` on unknown shapes.
    """
    if not url or not isinstance(url, str):
        raise SkillInstallError("empty or non-string URL")

    stripped = url.strip()

    # Bare local path first: a path that exists on disk wins over URL
    # parsing even if it looks like "github.com" somehow.
    local = Path(stripped).expanduser()
    if local.exists():
        return _resolve_local_path(local, name_hint)

    parsed = urlparse(stripped)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if host.endswith("agentskills.io"):
        if agentskills_resolver is None:
            raise SkillInstallError(
                "agentskills.io URLs require an agentskills_resolver"
            )
        target = agentskills_resolver(stripped)
        return resolve_from_url(target, name_hint=name_hint)

    lower_path = path.lower()

    # Check raw SKILL.md before github: a raw.githubusercontent URL is
    # host=github-ish but always ends .md, and should take that path.
    if lower_path.endswith("skill.md"):
        return _resolve_raw_md(stripped, name_hint, parsed)

    if lower_path.endswith(".zip"):
        return _resolve_zip(stripped, name_hint, parsed)

    if host.endswith("github.com"):
        return _resolve_github(stripped, name_hint, parsed)

    raise SkillInstallError(
        f"unrecognised skill install URL: {stripped!r}. "
        f"Supported: github.com (repo or /tree/<ref>/<path>), "
        f"raw SKILL.md URL, *.zip URL, agentskills.io/skills/<slug>, "
        f"or a local path"
    )


def _resolve_github(
    url: str, name_hint: str | None, parsed
) -> ResolvedSkill:
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise SkillInstallError(
            f"github URL missing owner/repo: {url!r}"
        )
    owner, repo = parts[0], parts[1].removesuffix(".git")
    clone_url = f"https://github.com/{owner}/{repo}"

    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
        subpath = "/".join(parts[4:]) if len(parts) > 4 else ""
        if not subpath:
            # Just a ref with no subpath → treat as whole repo at ref.
            name = _choose_name(name_hint, repo)
            return ResolvedSkill(
                source_type="github_repo",
                source_url=url,
                staging_name=name,
                clone_url=clone_url,
                ref=ref,
            )
        leaf = subpath.rstrip("/").rsplit("/", 1)[-1]
        name = _choose_name(name_hint, leaf)
        return ResolvedSkill(
            source_type="github_subpath",
            source_url=url,
            staging_name=name,
            clone_url=clone_url,
            ref=ref,
            subpath=subpath,
        )

    name = _choose_name(name_hint, repo)
    return ResolvedSkill(
        source_type="github_repo",
        source_url=url,
        staging_name=name,
        clone_url=clone_url,
    )


def _resolve_raw_md(
    url: str, name_hint: str | None, parsed
) -> ResolvedSkill:
    parts = [p for p in parsed.path.split("/") if p]
    host = (parsed.netloc or "").lower()
    # raw.githubusercontent.com URLs look like
    # /<owner>/<repo>/<ref>/<path...>/SKILL.md — the repo name is the
    # informative hint, not the git ref (the immediate parent).
    if host == "raw.githubusercontent.com" and len(parts) >= 4:
        candidate = parts[1]
    elif len(parts) >= 2:
        candidate = parts[-2]
    elif parts:
        candidate = parts[0].removesuffix(".md")
    else:
        candidate = "skill"
    name = _choose_name(name_hint, candidate)
    return ResolvedSkill(
        source_type="raw_md",
        source_url=url,
        staging_name=name,
    )


def _resolve_zip(
    url: str, name_hint: str | None, parsed
) -> ResolvedSkill:
    parts = [p for p in parsed.path.split("/") if p]
    stem = parts[-1].removesuffix(".zip") if parts else "skill"
    name = _choose_name(name_hint, stem)
    return ResolvedSkill(
        source_type="zip",
        source_url=url,
        staging_name=name,
    )


def _resolve_local_path(
    path: Path, name_hint: str | None
) -> ResolvedSkill:
    if path.is_dir():
        if not (path / "SKILL.md").is_file():
            raise SkillInstallError(
                f"local directory has no SKILL.md: {path}"
            )
        candidate = path.name
    elif path.suffix == ".md":
        candidate = path.stem
    else:
        raise SkillInstallError(
            f"unsupported local path (need dir with SKILL.md or a .md file): {path}"
        )
    return ResolvedSkill(
        source_type="local_path",
        source_url=str(path),
        staging_name=_choose_name(name_hint, candidate),
        local_path=path,
    )


# ---------------------------------------------------------------------------
# install_resolved — fetch + place under target_dir
# ---------------------------------------------------------------------------


def install_resolved(
    resolved: ResolvedSkill,
    *,
    target_dir: Path | None = None,
    http_fetcher: Callable[[str], str] | None = None,
    git_cloner: Callable[[str, Path, str | None], None] | None = None,
    zip_fetcher: Callable[[str], bytes] | None = None,
    force: bool = False,
    trust_tier: str | None = None,
) -> Path:
    """Perform the fetch described by *resolved* and install into ``target_dir``.

    Returns the path to the installed ``SKILL.md``. The final skill
    directory name comes from the skill's own frontmatter ``name``,
    not the URL — this mirrors ``kiso skill add`` and guarantees
    runtime lookup consistency.

    *trust_tier*, when provided, is recorded in ``.provenance.json``
    alongside the source metadata.
    """
    target_root = target_dir if target_dir is not None else _DEFAULT_SKILLS_DIR
    target_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="kiso-skill-") as staging:
        staging_dir = Path(staging)
        skill_md = _fetch_into_staging(
            resolved, staging_dir, http_fetcher, git_cloner, zip_fetcher
        )
        parsed = parse_skill_file(skill_md)
        if parsed is None:
            raise SkillInstallError(
                f"source is not a valid Agent Skill "
                f"(frontmatter / naming / required fields): {resolved.source_url}"
            )

        dest_dir = target_root / parsed.name
        if dest_dir.exists():
            if not force:
                raise SkillInstallError(
                    f"skill already installed at {dest_dir} — pass --force to overwrite"
                )
            shutil.rmtree(dest_dir)

        if resolved.source_type == "raw_md":
            dest_dir.mkdir()
            shutil.copy2(skill_md, dest_dir / "SKILL.md")
        else:
            skill_root = skill_md.parent
            shutil.copytree(skill_root, dest_dir)

        write_provenance(dest_dir, resolved, trust_tier=trust_tier)
        return dest_dir / "SKILL.md"


def _fetch_into_staging(
    resolved: ResolvedSkill,
    staging: Path,
    http_fetcher: Callable[[str], str] | None,
    git_cloner: Callable[[str, Path, str | None], None] | None,
    zip_fetcher: Callable[[str], bytes] | None,
) -> Path:
    """Populate *staging* with the source content; return path to SKILL.md."""
    if resolved.source_type == "raw_md":
        fetcher = http_fetcher or _default_http_fetcher
        body = fetcher(resolved.source_url)
        target = staging / "SKILL.md"
        target.write_text(body, encoding="utf-8")
        return target

    if resolved.source_type == "github_repo":
        cloner = git_cloner or _default_git_cloner
        repo_root = staging / "repo"
        cloner(resolved.clone_url, repo_root, resolved.ref)
        return _locate_skill_md_in_repo(repo_root, resolved)

    if resolved.source_type == "github_subpath":
        cloner = git_cloner or _default_git_cloner
        repo_root = staging / "repo"
        cloner(resolved.clone_url, repo_root, resolved.ref)
        sub = repo_root / resolved.subpath
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            raise SkillInstallError(
                f"subpath has no SKILL.md: {resolved.subpath}"
            )
        return skill_md

    if resolved.source_type == "zip":
        fetcher = zip_fetcher or _default_zip_fetcher
        data = fetcher(resolved.source_url)
        with zipfile.ZipFile(BytesIO(data)) as zf:
            zf.extractall(staging / "unpacked")
        return _locate_skill_md_in_repo(staging / "unpacked", resolved)

    if resolved.source_type == "local_path":
        src = resolved.local_path
        if src.is_dir():
            return src / "SKILL.md"
        copied = staging / src.name
        shutil.copy2(src, copied)
        return copied

    raise SkillInstallError(
        f"unknown source_type: {resolved.source_type!r}"
    )


def _locate_skill_md_in_repo(
    root: Path, resolved: ResolvedSkill
) -> Path:
    """Find the SKILL.md in a repo or unpacked zip.

    Rules:
    1. Top-level ``SKILL.md`` wins.
    2. Otherwise exactly one ``skills/<name>/SKILL.md`` → that one.
    3. Multiple ``skills/<name>/SKILL.md`` → error; user must narrow
       via a ``/tree/<ref>/skills/<name>`` URL.
    """
    top = root / "SKILL.md"
    if top.is_file():
        return top
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        candidates = sorted(
            sub for sub in skills_dir.iterdir()
            if sub.is_dir() and (sub / "SKILL.md").is_file()
        )
        if len(candidates) == 1:
            return candidates[0] / "SKILL.md"
        if len(candidates) > 1:
            names = [c.name for c in candidates]
            raise SkillInstallError(
                f"repo contains multiple skills ({names}); narrow the URL "
                f"with /tree/<ref>/skills/<name>"
            )
    raise SkillInstallError(
        f"no SKILL.md found in {resolved.source_url}"
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def write_provenance(
    skill_dir: Path,
    resolved: ResolvedSkill,
    *,
    trust_tier: str | None = None,
) -> None:
    """Write ``.provenance.json`` next to ``SKILL.md``.

    Captures the source URL, type, trust tier, and install time so
    users (and ``kiso skill info``) can tell where a skill came from.
    """
    data: dict = {
        "source_url": resolved.source_url,
        "source_type": resolved.source_type,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if resolved.ref:
        data["ref"] = resolved.ref
    if resolved.subpath:
        data["subpath"] = resolved.subpath
    if trust_tier is not None:
        data["trust_tier"] = trust_tier
    (skill_dir / PROVENANCE_FILE).write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Default fetchers (production)
# ---------------------------------------------------------------------------


def _default_http_fetcher(url: str) -> str:
    import httpx

    r = httpx.get(url, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _default_zip_fetcher(url: str) -> bytes:
    import httpx

    r = httpx.get(url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _default_git_cloner(url: str, dest: Path, ref: str | None) -> None:
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd.extend(["--branch", ref])
    cmd.extend([url, str(dest)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SkillInstallError(
            f"git clone failed: {result.stderr.strip()}"
        )


def _default_agentskills_resolver(url: str) -> str:
    import httpx

    r = httpx.head(url, timeout=15.0, follow_redirects=False)
    r.raise_for_status()
    loc = r.headers.get("location")
    if not loc:
        raise SkillInstallError(
            f"agentskills.io did not return a redirect for {url!r} "
            f"(status {r.status_code}); paste the backing github URL directly"
        )
    return loc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _choose_name(name_hint: str | None, fallback: str) -> str:
    if name_hint is not None:
        return _sanitize_name(name_hint)
    return _sanitize_name(fallback)


def _sanitize_name(raw: str) -> str:
    cleaned = _NAME_SANITIZE_RE.sub("-", raw.lower()).strip("-")
    if not cleaned:
        return "skill"
    if not (cleaned[0].isalnum()):
        cleaned = "s" + cleaned
    return cleaned[:64]
