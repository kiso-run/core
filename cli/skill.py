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

    try:
        path = install_resolved(
            resolved,
            target_dir=SKILLS_DIR,
            http_fetcher=_http_fetcher,
            git_cloner=_git_cloner,
            zip_fetcher=_zip_fetcher,
            force=args.force,
        )
    except SkillInstallError as exc:
        die(str(exc))

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
# Helpers
# ---------------------------------------------------------------------------


def _find_skill(name: str):
    for s in discover_skills(SKILLS_DIR):
        if s.name == name:
            return s
    return None
