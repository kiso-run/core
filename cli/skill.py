"""``kiso skill`` subcommand — manage Agent Skills in ``~/.kiso/skills/``.

Skills are role-scoped planner/worker/reviewer/messenger instructions
(Agent Skills format: Markdown with YAML frontmatter). See
``kiso/skill_loader.py`` for the discovery + parse contract and
``docs/skills.md`` for the user-facing guide.

Ships the local-only lifecycle: ``list``, ``info``, ``add`` (from a
directory or single ``.md`` file), ``remove``. URL-based install and
skill testing are separate subcommands added by later milestones.

The argparse / dispatcher shape mirrors ``cli/mcp.py`` so the two
plugin types expose the same ergonomics.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from cli.render import die
from kiso.config import SKILLS_DIR as _DEFAULT_SKILLS_DIR
from kiso.skill_loader import (
    discover_skills,
    invalidate_skills_cache,
    parse_skill_file,
)
from kiso.skill_install import (
    ResolvedSkill,
    SkillInstallError,
    install_resolved,
    resolve_from_url,
)

SKILLS_DIR: Path = _DEFAULT_SKILLS_DIR

# Test hooks — overridden by tests to inject offline fetchers.
_http_fetcher = None
_git_cloner = None
_zip_fetcher = None
_agentskills_resolver = None


def format_skill_install_http_failure(
    *,
    url: str,
    status: int,
    reason: str,
) -> str:
    """User-facing message for an HTTP failure during ``kiso skill install``.

    Names the URL that was tried and the remote status so the user
    can tell "the URL was wrong" from "the server is down" from "the
    file does not exist". Paired with the pre-argparse
    ``--from-url`` hint (``cli/_from_url_hint.py``) for bare-identifier
    mistakes.
    """
    return (
        f"skill install failed: HTTP {status} {reason}\n"
        f"  URL: {url}\n"
        f"  Next step: check the URL in a browser or retry with a "
        f"different --from-url source."
    )


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def add_subcommands(parent: argparse.ArgumentParser) -> None:
    s = parent.add_subparsers(dest="skill_command")

    s.add_parser("list", help="list installed skills")

    info = s.add_parser("info", help="show a skill's metadata and role sections")
    info.add_argument("name", help="skill name")

    add = s.add_parser(
        "add",
        help="copy a local skill (directory or single .md) into ~/.kiso/skills/",
    )
    add.add_argument("path", help="path to a skill directory or single .md file")
    add.add_argument(
        "--yes",
        "-y",
        action="store_true",
        dest="yes",
        help="overwrite an existing skill of the same name",
    )

    rm = s.add_parser("remove", help="remove an installed skill")
    rm.add_argument("name", help="skill name")
    rm.add_argument("--yes", "-y", action="store_true", help="skip confirmation")

    trust = s.add_parser("trust", help="manage install-time trust prefixes")
    t = trust.add_subparsers(dest="skill_trust_command")
    t.add_parser("list", help="list hardcoded + custom trust prefixes")
    ta = t.add_parser("add", help="add a user trust prefix")
    ta.add_argument("prefix", help="prefix (literal or glob ending with *)")
    tr = t.add_parser("remove", help="remove a user trust prefix")
    tr.add_argument("prefix", help="prefix to remove")

    tst = s.add_parser("test", help="audit an installed skill")
    tst.add_argument("name", help="skill name")

    install = s.add_parser(
        "install",
        help="install an Agent Skill from a URL (github / raw SKILL.md / zip / agentskills.io)",
    )
    install.add_argument("--from-url", dest="from_url", required=True, help="source URL")
    install.add_argument("--name", default=None, help="override the sanitized name")
    install.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print the install plan without executing",
    )
    install.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip confirmation prompts",
    )
    install.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing skill of the same name",
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def handle(args: argparse.Namespace) -> int:
    cmd = getattr(args, "skill_command", None)
    if cmd is None:
        print("usage: kiso skill {list|info|add|remove|install}")
        return 2
    if cmd == "list":
        return _cmd_list()
    if cmd == "info":
        return _cmd_info(args)
    if cmd == "add":
        return _cmd_add(args)
    if cmd == "remove":
        return _cmd_remove(args)
    if cmd == "install":
        return _cmd_install(args)
    if cmd == "trust":
        return _cmd_trust(args)
    if cmd == "test":
        return _cmd_test(args)
    die(f"unknown skill subcommand: {cmd}")
    return 2  # unreachable


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list() -> int:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    skills = discover_skills(SKILLS_DIR)
    if not skills:
        print("(no skills installed)")
        print(f"Skills dir: {SKILLS_DIR}")
        return 0
    name_w = max(len(s.name) for s in skills)
    name_w = max(name_w, 4)
    print(f"{'NAME':<{name_w}}  {'SOURCE':<9}  DESCRIPTION")
    for s in skills:
        source = "directory" if s.bundled_root is not None else "file"
        detail = s.description.strip().splitlines()[0] if s.description else ""
        print(f"{s.name:<{name_w}}  {source:<9}  {detail}")
    return 0


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def _cmd_info(args: argparse.Namespace) -> int:
    skill = _find_skill(args.name)
    if skill is None:
        die(f"no such skill: {args.name!r}")

    print(f"name: {skill.name}")
    print(f"description: {skill.description}")
    if skill.version:
        print(f"version: {skill.version}")
    if skill.license:
        print(f"license: {skill.license}")
    if skill.compatibility:
        print(f"compatibility: {skill.compatibility}")
    if skill.when_to_use:
        print(f"when_to_use: {skill.when_to_use}")
    if skill.audiences:
        print(f"audiences: {', '.join(skill.audiences)}")
    if skill.allowed_tools:
        print(f"allowed-tools: {skill.allowed_tools}")
    if skill.activation_hints:
        print(f"activation_hints: {skill.activation_hints}")
    if skill.metadata:
        print(f"metadata: {skill.metadata}")
    if skill.bundled_root is not None:
        print(f"source: {skill.bundled_root} (directory)")
    elif skill.path is not None:
        print(f"source: {skill.path} (single file)")

    if skill.role_sections:
        print()
        print("--- role sections ---")
        for role in ("planner", "worker", "reviewer", "messenger"):
            body = skill.role_sections.get(role)
            if not body:
                continue
            print()
            print(f"## {role.capitalize()}")
            print(body)
    elif skill.body:
        print()
        print("--- body (no role headings; applies to planner) ---")
        print(skill.body)
    return 0


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    src = Path(args.path).expanduser()
    if not src.exists():
        die(f"source path does not exist: {src}")

    # Validate via the canonical loader so naming + frontmatter rules
    # stay in one place. parse_skill_file returns None on any problem
    # and logs the reason; we re-check to give the user a hard error.
    if src.is_dir():
        skill_md = src / "SKILL.md"
        if not skill_md.is_file():
            die(f"directory has no SKILL.md: {src}")
        parsed = parse_skill_file(skill_md, bundled_root=src)
    elif src.suffix == ".md":
        parsed = parse_skill_file(src)
    else:
        die(f"unsupported source (need a directory with SKILL.md or a .md file): {src}")

    if parsed is None:
        die(
            f"source is not a valid Agent Skill: {src}\n"
            "  (check YAML frontmatter, required 'name' + 'description', "
            "and the naming rule: lowercase letters / digits / hyphens, "
            "1-64 chars, must start with a letter or digit)"
        )

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        dest = SKILLS_DIR / parsed.name
    else:
        dest = SKILLS_DIR / f"{parsed.name}.md"

    if dest.exists() and not args.yes:
        die(
            f"skill already installed at {dest} — pass --yes to overwrite"
        )

    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    else:
        if dest.exists():
            dest.unlink()
        shutil.copy2(src, dest)

    invalidate_skills_cache()
    print(f"installed skill {parsed.name!r} → {dest}")
    return 0


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def _cmd_remove(args: argparse.Namespace) -> int:
    dir_target = SKILLS_DIR / args.name
    file_target = SKILLS_DIR / f"{args.name}.md"

    if dir_target.is_dir():
        target = dir_target
    elif file_target.is_file():
        target = file_target
    else:
        die(f"no such skill: {args.name!r}")

    if not args.yes:
        print(f"About to remove {target}")
        confirm = input("confirm? [y/N] ").strip().lower()
        if confirm != "y":
            print("aborted")
            return 1

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    invalidate_skills_cache()
    print(f"removed skill {args.name!r}")
    return 0


# ---------------------------------------------------------------------------
# install --from-url
# ---------------------------------------------------------------------------


def _cmd_install(args: argparse.Namespace) -> int:
    from kiso import skill_trust

    try:
        resolved = resolve_from_url(
            args.from_url,
            name_hint=args.name,
            agentskills_resolver=_agentskills_resolver,
        )
    except SkillInstallError as exc:
        die(str(exc))

    _print_install_plan(resolved)
    if args.dry_run:
        print("(dry run; no changes written)")
        return 0

    source_key = skill_trust.source_key_for_url(args.from_url)
    tier = skill_trust.is_trusted(source_key)
    if tier == "untrusted" and not args.yes:
        print(f"\n⚠  untrusted source: {source_key}")
        print(
            "   Tier 1 prefixes are hardcoded (Anthropic / kiso-run);"
            " add your own via `kiso skill trust add`."
        )
        confirm = input("Install anyway? [y/N] ").strip().lower()
        if confirm != "y":
            print("aborted")
            return 1
    trust_tier_for_provenance = (
        tier if tier != "untrusted" else "untrusted-user-approved"
    )

    try:
        path = install_resolved(
            resolved,
            target_dir=SKILLS_DIR,
            http_fetcher=_http_fetcher,
            git_cloner=_git_cloner,
            zip_fetcher=_zip_fetcher,
            force=args.force,
            trust_tier=trust_tier_for_provenance,
        )
    except SkillInstallError as exc:
        die(str(exc))

    risks = skill_trust.detect_risk_factors(path.parent)
    if risks:
        print("risk factors detected:")
        for r in risks:
            print(f"  - {r}")

    invalidate_skills_cache()
    print(f"installed skill → {path.parent}")
    return 0


def _print_install_plan(resolved: ResolvedSkill) -> None:
    print(f"Resolved skill: {resolved.staging_name}")
    print(f"  source: {resolved.source_url}")
    print(f"  type: {resolved.source_type}")
    if resolved.clone_url:
        print(f"  clone: {resolved.clone_url}")
    if resolved.ref:
        print(f"  ref: {resolved.ref}")
    if resolved.subpath:
        print(f"  subpath: {resolved.subpath}")
    if resolved.local_path:
        print(f"  local: {resolved.local_path}")


# ---------------------------------------------------------------------------
# trust — user-extensible install-time allowlist
# ---------------------------------------------------------------------------


def _cmd_trust(args: argparse.Namespace) -> int:
    from kiso.skill_trust import SKILL_TIER1_PREFIXES
    from kiso.trust_store import add_prefix, load_trust_store, remove_prefix

    sub = getattr(args, "skill_trust_command", None)
    if sub is None or sub == "list":
        print("tier1 (hardcoded):")
        for p in SKILL_TIER1_PREFIXES:
            print(f"  {p}")
        store = load_trust_store()
        if store.skill:
            print("custom (user):")
            for p in store.skill:
                print(f"  {p}")
        else:
            print("custom (user): (none)")
        return 0
    if sub == "add":
        add_prefix("skill", args.prefix)
        print(f"added skill trust prefix: {args.prefix}")
        return 0
    if sub == "remove":
        remove_prefix("skill", args.prefix)
        print(f"removed skill trust prefix: {args.prefix}")
        return 0
    die(f"unknown trust subcommand: {sub}")
    return 2


# ---------------------------------------------------------------------------
# test — local skill audit
# ---------------------------------------------------------------------------


def _cmd_test(args: argparse.Namespace) -> int:
    skill_path = _resolve_skill_path(args.name)
    if skill_path is None:
        die(f"no such skill: {args.name!r}")

    skill_md = skill_path / "SKILL.md" if skill_path.is_dir() else skill_path
    parsed = parse_skill_file(skill_md)
    if parsed is None:
        print(f"✗ {args.name}: frontmatter invalid or missing required fields")
        return 1

    warnings: list[str] = []
    warnings.extend(_check_referenced_paths(skill_md, skill_path))
    warnings.extend(_check_allowed_tools(parsed))

    print(f"✓ {args.name}: frontmatter ok (name={parsed.name})")
    if parsed.role_sections:
        roles = ", ".join(sorted(parsed.role_sections.keys()))
        print(f"  role sections: {roles}")
    if warnings:
        print("warnings:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("  no warnings")
    return 0


_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _check_referenced_paths(skill_md: Path, skill_root: Path) -> list[str]:
    warnings: list[str] = []
    body = skill_md.read_text(encoding="utf-8", errors="replace")
    for match in _MARKDOWN_LINK_RE.finditer(body):
        link = match.group(1).strip()
        if link.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if skill_root.is_dir():
            target = (skill_root / link).resolve()
            if not target.exists():
                warnings.append(f"broken relative link: {link}")
    return warnings


def _check_allowed_tools(skill) -> list[str]:
    if not skill.allowed_tools:
        return []
    warnings: list[str] = []
    for tok in _ALLOWED_TOOLS_BASH_RE.findall(skill.allowed_tools):
        binary = tok.split()[0]
        if binary and binary != "*" and shutil.which(binary) is None:
            warnings.append(
                f"allowed-tools references '{binary}' which is not on PATH"
            )
    return warnings


_ALLOWED_TOOLS_BASH_RE = re.compile(r"Bash\(\s*([^)]+?)\s*\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_skill(name: str):
    for s in discover_skills(SKILLS_DIR):
        if s.name == name:
            return s
    return None


def _resolve_skill_path(name: str) -> Path | None:
    dir_target = SKILLS_DIR / name
    file_target = SKILLS_DIR / f"{name}.md"
    if dir_target.is_dir():
        return dir_target
    if file_target.is_file():
        return file_target
    return None
