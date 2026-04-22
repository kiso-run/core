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

kiso skill install --from-url <url>                 # install from a URL
kiso skill install --from-url <url> --name foo      # override the skill name
kiso skill install --from-url <url> --dry-run       # print plan, don't fetch
kiso skill install --from-url <url> --force         # overwrite if already installed
```

`add` accepts either a skill directory (with `SKILL.md` inside)
or a single `.md` file. The canonical loader validates naming
and frontmatter before copying, so anything that installs cleanly
will also parse cleanly at runtime.

## URL forms for `install --from-url`

All URL forms are resolved offline into a normalised plan; network
calls happen only when the plan is executed (skip with `--dry-run`).

- **Github repo** — `https://github.com/<owner>/<repo>`: git-clone
  the whole repo. If there's a top-level `SKILL.md`, the entire
  repo becomes the skill (sibling files come along); if there's
  exactly one `skills/<name>/SKILL.md`, that subdirectory is
  installed. A repo with multiple `skills/*` entries fails with a
  hint to narrow the URL.
- **Github tree subpath** — `https://github.com/<owner>/<repo>/tree/<ref>/<path>`:
  clone at `<ref>`, install the named subpath. Use this when a
  kit repo ships multiple skills.
- **Raw `SKILL.md`** — any URL whose path ends `SKILL.md` or
  `skill.md`, e.g. `https://raw.githubusercontent.com/.../SKILL.md`:
  fetch the single file; the install becomes `<name>/SKILL.md`
  where `<name>` is taken from the skill's own frontmatter.
- **Zip archive** — `*.zip` URL: download + unpack; the archive
  must contain either a top-level `SKILL.md` or exactly one
  `skills/<name>/SKILL.md`.
- **`agentskills.io`** — `https://agentskills.io/skills/<slug>`:
  the resolver follows the redirect to the backing Github URL
  and re-runs the install from there.
- **Local path** — a path that exists on disk: delegates to the
  same copy path as `kiso skill add`.

Every URL install writes `<name>/.provenance.json` recording the
source URL, source type, optional git ref + subpath, and install
timestamp. Inspect with `kiso skill info <name>` once M1530's
metadata surface lands; for now the file is visible on disk.

The trust-tier surface (`kiso skill test`, untrusted-source
warnings, recommended-skills registry) is covered by M1515; this
milestone wires the hook point.

## See also

- [agentskills.io](https://agentskills.io) — the standard.
- `docs/cli.md` — CLI reference index.
- `docs/recommended-skills.md` — curated skills outside the
  default install.
- `kiso/skill_loader.py` — the canonical parser + discovery.
- `kiso/skill_runtime.py` — role-section projection at inference
  time.
