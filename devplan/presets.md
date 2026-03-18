# Preset Ecosystem — Deferred Items

> Core infra (M718, M723, M719, M730) in v0.7_wip.md Phase 44.
> Document reader tool (M755) in v0.7_wip.md Phase 45.

---

## Additional Presets (deferred — do after basic proves the pattern)

### M720 — `developer` preset

Tools: aider, websearch, browser. Behaviors: TDD, code review, git workflow.
Deferred: behaviors are opinionated — need user feedback on basic first.

### M721 — `researcher` preset

Tools: websearch, browser. Behaviors: source verification, structured synthesis.
Deferred: niche use case.

### M722 — `assistant` preset

Tools: gworkspace, websearch, browser. Behaviors: email drafts, calendar confirmation.
Deferred: depends on gworkspace tool maturity.

---

## MD Skills — status: on hold

MD skills (lightweight planner instructions in .md files) work for **workflow guidance**
(plan structure, task ordering, strategy) but NOT for **content quality** (the planner
delegates content generation to worker/tools which don't see skills).

The infrastructure exists and costs nothing when unused. No skills will be created
speculatively — only when a real use case demonstrates that a skill improves plan
quality for a specific workflow.
