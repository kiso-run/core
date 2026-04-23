# Tutorial — your first skill and your first MCP

This tutorial assumes you already ran the installer and `kiso doctor`
came back green. If not, start at [README — Quick start](../README.md#quick-start).

By the end of this walk-through you will have:

1. Written and installed **one Agent Skill** that nudges the planner
   on a recurring task.
2. Installed **one MCP server from a URL** and used it in a plan.
3. Watched a multi-task plan run end to end.

The two primitives — skills and MCP — are all you need to extend kiso.

## 1. Your first skill (authored locally)

An Agent Skill is a directory with a `SKILL.md` file. It teaches the
planner *how to think* about a class of problem, without adding any
new runtime capability. The minimal skill is a single file.

Create one:

```bash
mkdir -p ~/kiso-skills/release-notes
cat > ~/kiso-skills/release-notes/SKILL.md <<'MD'
---
name: release-notes
description: Write short, user-facing release notes from a list of commits.
applies_to:
  - release notes
  - changelog
---

## Planner

When the user asks for release notes from commits, structure the
plan as:
1. exec — gather commits (`git log --oneline main..HEAD`)
2. msg   — write the notes in user-facing language, one bullet per
   user-visible change; omit internal refactors.
MD
```

Install it with the local-directory form of `--from-url`:

```bash
kiso skill install --from-url file://$HOME/kiso-skills/release-notes
kiso skill list
```

`kiso skill list` should show `release-notes` with its trust tier.
Ask kiso to use it:

```bash
kiso msg "write release notes for the last 5 commits on this branch"
```

The planner will now pick the skill up via the briefer and follow
its two-step recipe. You can inspect the plan with `kiso status`.

### When a skill is the right tool

- The capability already exists (exec, filesystem MCP, search MCP) —
  you just need the planner to follow a specific approach.
- The guidance is reusable across multiple messages.
- No new binary or API connection is needed.

If you need something new (codegen, OCR, a bespoke SaaS API), that's
what MCP is for.

## 2. Your first MCP server (installed from a URL)

MCP servers are standardized capability providers. Kiso is a
*consumer*: you bring the URL, Kiso handles the rest.

Install the official GitHub MCP server from npm:

```bash
kiso mcp install --from-url npm:@modelcontextprotocol/server-github
kiso mcp env github set GITHUB_PERSONAL_ACCESS_TOKEN <your-token>
kiso mcp test github          # smoke-test
kiso mcp list
```

What happened:

- `kiso mcp install` added the server to `~/.kiso/instances/<name>/mcp.json`.
- `kiso mcp env` stored the token in the per-server env file (never
  in `config.toml`).
- `kiso mcp test` made a live call to confirm the server is healthy.
- `kiso mcp list` shows the installed servers and their method
  catalog.

Ask kiso to use it:

```bash
kiso msg "list the five most recent issues on <org>/<repo> and give me a one-line summary of each"
```

The planner will produce an `mcp` task that calls the GitHub server's
`list_issues` method, flow the results into the next task, and
deliver a summary.

### Install sources kiso understands

`kiso mcp install --from-url <source>` accepts:

- `npm:@scope/package` — npm packages
- `pypi:package` — PyPI packages
- `pulsemcp:<slug>` — the [PulseMCP](https://pulsemcp.com) index
- `https://github.com/<org>/<repo>[@tag]` — a git ref
- `https://…/server.json` — a hosted server.json manifest

Same conventions apply to `kiso skill install --from-url`.

## 3. A multi-task plan

Ask something that requires more than one step:

```bash
kiso msg "find the top 3 largest Python files in this repo, summarize what each one does, and write the summary to REPO_OVERVIEW.md"
```

The planner will emit something like:

1. `exec` — `find . -name "*.py" -printf "%s\t%p\n" | sort -rn | head -3`
2. `exec` — read each file (one exec task per file)
3. `exec` — write the summary to `REPO_OVERVIEW.md`
4. `msg`  — deliver the summary to you

Watch it run:

```bash
kiso status
```

If any step fails, the reviewer will decide whether a local retry or
a full replan is the right response. `kiso status` shows the replan
history. When the plan finishes, `REPO_OVERVIEW.md` will be in your
session workspace, linkable via the external URL printed by the
installer.

## Where to go next

- [docs/skills.md](skills.md) — full skill authoring guide (role
  sections, trust tiers, activation hints, install-from-URL
  semantics).
- [docs/mcp.md](mcp.md) — full MCP consumer guide (per-session
  pools, schema validation, recovery, Resources / Prompts /
  Sampling).
- [docs/default-preset.md](default-preset.md) — what's in
  `kiso init --preset default`.
- [docs/recommended-skills.md](recommended-skills.md) and
  [docs/recommended-mcps.md](recommended-mcps.md) — curated
  non-preset options with provenance disclaimers.
- [docs/cli.md](cli.md) — every `kiso` subcommand.
