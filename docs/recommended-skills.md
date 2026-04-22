# Recommended skills (outside the default)

Skills that are useful but not shipped in kiso's default install.
Each entry is informational — nothing here is auto-installed. Run
`kiso skill install --from-url <url>` to pull one in, or use the
chat-mediated install flow (once it lands) to let the planner
propose it.

## Format

Each entry lists:

- **Source** — URL you would paste into `kiso skill install --from-url`
- **Purpose** — one-line description of what the skill gives the
  planner/worker/reviewer/messenger
- **Trust tier** — `tier1` (hardcoded allowlist), `custom` (user
  must add a prefix via `kiso skill trust add`), or `community`
  (plain untrusted — explicit confirm required at install)
- **Risk factors** — what `kiso skill install` and
  `kiso skill test` will surface about this skill

## Tier 1 — Anthropic / kiso-run curated

These match the hardcoded Tier 1 prefixes in
`kiso/skill_trust.py` (`github.com/anthropics/skills/*`,
`github.com/kiso-run/skills/*`). They install silently.

### `python-debug`

- **Source**: `https://github.com/anthropics/skills/tree/main/python-debug`
  *(example — check the upstream repo for the current path)*
- **Purpose**: structured traceback reading, isolate-then-fix
  pattern, pytest hints for worker + reviewer
- **Trust tier**: `tier1`
- **Risk factors**: none expected

### `writing-style`

- **Source**: `https://github.com/anthropics/skills/tree/main/writing-style`
- **Purpose**: tone and voice guidance for the messenger role
- **Trust tier**: `tier1`
- **Risk factors**: none expected

### `deploy-runbook`

- **Source**: `https://github.com/kiso-run/skills/tree/main/deploy-runbook`
  *(example — forthcoming in the kiso-run/skills repo)*
- **Purpose**: deploy / rollback / incident-response runbooks
  projected into planner + worker sections
- **Trust tier**: `tier1`
- **Risk factors**: may include `scripts/` for canary checks; the
  CLI will report this at install time

## Community skills (Tier 2 / untrusted)

Community skills install only after an explicit `Install anyway?
[y/N]` confirm (or `--yes`). You can elevate a trusted author to
no-prompt status with `kiso skill trust add github.com/<author>/*`.

### `agentskills.io` registry

- **Source**: `https://agentskills.io/skills/<slug>`
- **Purpose**: community catalog of Agent Skills published under
  the `agentskills.io` standard. The resolver follows the
  redirect to the backing github repo; the usual trust gate then
  applies.
- **Trust tier**: `community` by default; pin a vetted author
  with `kiso skill trust add github.com/<author>/*`
- **Risk factors**: whatever the skill actually bundles —
  `scripts/`, wide `allowed-tools`, oversized assets. Run
  `kiso skill test <name>` after install to audit.

### Community examples (illustrative)

Community skills are listed here purely as discovery hints;
check the upstream repo before installing.

- **code-review-checklist** — structured review prompts for the
  reviewer role. *(community)*
- **incident-postmortem** — postmortem template + messenger
  guidance. *(community)*
- **diagram-prompts** — mermaid / sequence / ER diagram
  conventions for the worker. *(community)*

## Discovery

- [agentskills.io](https://agentskills.io) — the standard's
  website and community index
- [GitHub topic: `agent-skills`](https://github.com/topics/agent-skills)
- `https://github.com/anthropics/skills` — Anthropic's curated
  set (Tier 1)
- `https://github.com/kiso-run/skills` — kiso-run curated set
  (Tier 1, forthcoming)

## Installing

```bash
# Tier 1 — silent install
kiso skill install --from-url https://github.com/anthropics/skills/tree/main/python-debug

# Community — explicit confirm
kiso skill install --from-url https://github.com/some-author/my-skill

# Community — skip the prompt
kiso skill install --from-url https://github.com/some-author/my-skill --yes

# Elevate a specific author to custom-trust (no prompt)
kiso skill trust add github.com/trusted-author/*
```

## Auditing after install

```bash
kiso skill test <name>
```

Runs frontmatter validation, checks that relative markdown links
resolve, and warns on `allowed-tools` binaries that aren't on
PATH. Exits non-zero on hard failures (bad frontmatter, missing
required fields, invalid skill name); warnings still exit 0.

**Safety reminder**: skills can declare `scripts/` and
`allowed-tools` that expand what the agent is willing to
execute. `kiso skill install` detects these and reports them
alongside the trust gate — review before approving.
