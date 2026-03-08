# Skill Development — Devplan Standard

Every skill should include a `DEVPLAN.md` in its root directory. This ensures
that improvement requests and feature additions are structured, traceable,
and actionable by automated agents.

## Why a devplan per skill?

- **Traceability:** Each change has a numbered milestone with rationale.
- **Agent-friendly:** Automated agents can read the devplan and execute
  milestones autonomously using the devplan-executor workflow.
- **Handoff-ready:** A new developer (human or agent) can pick up where
  the previous one left off.

## DEVPLAN.md Structure

```markdown
# {Skill Name} — Development Plan

## Overview
What the skill does, current capabilities, known limitations.

## Architecture
Key files, dependencies, kiso integration points.

- `run.py` — entry point, argument parsing
- `kiso.toml` — manifest with args schema
- `deps.sh` — system dependency installer

## Capabilities

| Action | Description | Args | Output | Status |
|--------|-------------|------|--------|--------|
| navigate | Open URL in browser | url: string | screenshot path | ✅ |
| search | Web search | query: string | results JSON | ✅ |
| click | Click element | selector: string | screenshot path | planned |

## Milestones

Numbered from M1 (per-skill, independent of core numbering).

### M1 — Feature name

**Problem:** What's missing or broken.

**Change:**
1. Implementation task
2. Tests
3. Update capabilities table

### M2 — Next feature
...

## Milestone Checklist
- [x] **M1** — Feature name ✅
- [ ] **M2** — Next feature

## Known Issues / Improvement Ideas

Bullet list with enough context for an agent to pick up and implement:

- Screenshots are PNG only, add WebP support for smaller files
- Timeout handling is missing for slow page loads
- Add retry logic for flaky network connections
```

## Conventions

- **Milestone numbers are per-skill.** M1 in the browser skill is unrelated
  to M1 in the search skill.
- **Keep it actionable.** Each milestone should be implementable in a single
  session by an agent.
- **Update the capabilities table** when adding or changing actions.
- **Improvement requests go in the devplan**, not as ad-hoc instructions.
  Write them as milestones with Problem/Change structure.
- **Mark completed milestones** with `[x]` and ✅.

## Skill file structure reminder

```
skills/{name}/
├── kiso.toml           # manifest (required)
├── pyproject.toml      # python dependencies (required)
├── run.py              # entry point (required)
├── deps.sh             # system deps installer (optional)
├── DEVPLAN.md          # development plan (recommended)
├── README.md           # docs for humans (optional)
└── .venv/              # created by uv on install
```

See [skills.md](skills.md) for the full skill specification.
