# Devplan Format — kiso core

Standard format for development plans in the kiso core repository.

## Structure

### Header

```markdown
# Devplan v{X.Y} — {Theme}

## Problem Statement
2-3 sentences explaining what this devplan addresses and why.
```

### Problem Inventory (optional)

Numbered problems (P1, P2, ...) describing systemic issues.
Each problem has a title and 3-5 line description of root cause.
Milestones reference these via "Problem: P1".

```markdown
## Problems

### P1 — Context overload
Models receive irrelevant rules and data. Causes instruction-following
degradation, especially on weaker models.

### P2 — Blind truncation
_truncate_output cuts at N chars, destroying information asymmetrically.
```

### Architecture (optional)

High-level design when the devplan introduces new architecture.
Include context flow diagrams, schema definitions, and key decisions.

### Milestones

Each milestone is a self-contained, testable change:

```markdown
### M{N} — {Short name}

**Problem:** What's wrong (or reference to P{N} above).

**Files:** `path/to/file.py` — brief description of changes

**Change:**

1. First concrete change to make
2. Second concrete change

**Test:** What tests to write/run to verify the change.
```

### Milestone Checklist

Summary at the bottom, grouped by phase:

```markdown
## Milestone Checklist

### Phase 1: Foundation
1. [ ] **M242** — Briefer role
2. [ ] **M243** — Planner modularization

### Phase 2: Integration
3. [ ] **M244** — Briefer for planner
```

Mark `[x]` and add ` ✅` when complete.

## Conventions

- **Milestone numbers are global and never reused.** M1-M148 in v0.2,
  M149-M241 in v0.3, M242+ in v0.4. New devplans continue the sequence.
- **One devplan per major theme.** Close and start a new one when done.
- **`DEVPLAN.md` in repo root** is a pointer to the active devplan file
  in `devplan/`.
- **Completed devplans** stay in `devplan/` for reference (rename from
  `_wip.md` to `.md`).
- **Each milestone must be independently testable.** Don't combine
  unrelated changes.
- **Problem → Files → Change → Test** structure ensures milestones are
  actionable by automated agents.

## Naming

- Active devplan: `devplan/v{X.Y}_wip.md`
- Completed devplan: `devplan/v{X.Y}.md`
- Pointer file: `DEVPLAN.md` (contains path to active devplan)
