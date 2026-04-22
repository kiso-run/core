# Agent Skills

Skills are the role-scoped instruction primitive for kiso's
planner / worker / reviewer / messenger. They follow the standard
Agent Skills format from [agentskills.io](https://agentskills.io):
Markdown with YAML frontmatter.

A skill tells the planner *when* to apply a piece of guidance, the
worker *how* to execute it, the reviewer *what* to check, and the
messenger *how* to phrase the reply. One file, one behaviour, four
role slices — the skill runtime projects each slice into the
matching role prompt at inference time.

## Where skills live

```
~/.kiso/skills/
  python-debug/
    SKILL.md
    scripts/
    references/
    assets/
  terse.md
```

Two accepted shapes:

- **Directory skill** — `<name>/SKILL.md` plus optional
  `scripts/`, `references/`, `assets/`. Canonical form; use this
  when the skill needs bundled files.
- **Single-file skill** — `<name>.md` at the skills root. Use
  this for lightweight, purely-textual guidance.

Directory skills take precedence when both shapes exist for the
same name.

## Skill name rule

Names follow the Agent Skills standard: lowercase letters, digits,
and hyphens, 1-64 characters, must start with a letter or digit.
The kiso loader rejects non-conforming names with a warning.

Examples: `python-debug`, `writing-style-kit`, `deploy-runbook`,
`terse`.

## SKILL.md structure

```markdown
---
name: python-debug
description: Helps debug Python exceptions and stack traces.
license: MIT
compatibility: ">=1.0"
metadata:
  author: team
allowed-tools: Bash(python -m pytest *) Read
when_to_use: User reports a Python traceback or import error.
audiences: [planner, worker]
activation_hints:
  applies_to: [python, traceback, exception]
  excludes: [javascript]
version: "0.1.0"
---

Read the traceback, isolate the failing frame, propose a fix.

## Planner
Break the bug into reproduce, isolate, fix, verify.

## Worker
Run `python -m pytest -x -q` and capture stderr.

## Reviewer
Output includes a diff or a passing test log.

## Messenger
Summarise the root cause in one sentence, then the fix.
```

### Frontmatter — standard fields

- **`name`** (required) — the skill's canonical name.
- **`description`** (required) — one-line summary used by `kiso
  skill list` and by the planner's activation pre-filter.
- **`license`** — SPDX identifier or free text.
- **`compatibility`** — version constraint string.
- **`metadata`** — free-form mapping for author / source / tags.
- **`allowed-tools`** — Agent Skills standard tool-scope string
  (passed through verbatim; not enforced by the kiso runtime
  today).

### Frontmatter — Kiso extensions

- **`when_to_use`** — a natural-language trigger. Helps the planner
  choose between skills when the description is terse.
- **`audiences`** — list of roles this skill applies to. Defaults
  to planner-only when role headings are absent.
- **`activation_hints`** — `{applies_to: [...], excludes: [...]}`
  keyword lists consumed by the deterministic pre-filter that
  runs before the briefer.
- **`version`** — a version string of the skill itself (unrelated
  to kiso's package version).

### Role sections

Headings `## Planner`, `## Worker`, `## Reviewer`, `## Messenger`
split the body into role-scoped sections. Each section is
projected into the corresponding role's prompt; content before the
first role heading belongs to the generic body and defaults to
planner-only guidance.

Only the four known roles are extracted. Other `## ...` headings
in the body are treated as part of whichever role section
surrounds them.

## CLI

```bash
kiso skill list                       # list installed skills
kiso skill info <name>                # show metadata + role sections
kiso skill add <path>                 # copy a local dir or .md into ~/.kiso/skills/
kiso skill add <path> --yes           # overwrite an existing skill of the same name
kiso skill remove <name>              # remove an installed skill
kiso skill remove <name> --yes        # skip the confirmation prompt
```

`add` accepts either a skill directory (with `SKILL.md` inside)
or a single `.md` file. The canonical loader validates naming
and frontmatter before copying, so anything that installs cleanly
will also parse cleanly at runtime.

URL-based install (`kiso skill install --from-url …`) and
`kiso skill test` are handled by separate subcommands layered on
the trust tiers; see `docs/recommended-skills.md` for curated
sources once those land.

## See also

- [agentskills.io](https://agentskills.io) — the standard.
- `docs/cli.md` — CLI reference index.
- `docs/recommended-skills.md` — curated skills outside the
  default install.
- `kiso/skill_loader.py` — the canonical parser + discovery.
- `kiso/skill_runtime.py` — role-section projection at inference
  time.
