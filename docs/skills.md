# MD Skills

MD skills are lightweight planner instructions stored as markdown files. They shape **how kiso plans tasks**, not how it executes them.

## How It Works

```
~/.kiso/skills/
├── research-workflow.md
└── debug-strategy.md
```

Each file has YAML frontmatter and a markdown body:

```markdown
---
name: research-workflow
summary: Multi-source research with cross-verification
---

When researching a topic:
1. Search at least 2 independent sources
2. Cross-verify key claims before reporting
3. Structure output as: findings, evidence, caveats
```

### Pipeline

1. `discover_md_skills()` scans `~/.kiso/skills/` for `.md` files (cached 30s)
2. The **briefer** decides which skills are relevant for the current request
3. Selected skill instructions are injected into the **planner prompt**
4. The planner reads them and incorporates them into the plan structure

Skills are additive context — they never override core planner rules.

## What Skills CAN Influence

Skills shape the **plan** — the sequence of tasks the planner creates:

- **Task ordering**: "search first, then verify, then report"
- **Strategy**: "use 2+ sources", "backup before modifying"
- **Tool selection**: "prefer websearch over browser for research"
- **Task detail specificity**: more detailed exec descriptions → better worker output

### Example: Good Skill

```markdown
---
name: debug-strategy
summary: Systematic debugging — reproduce first, then fix
---

When debugging a reported bug:
1. Plan an exec task to reproduce the bug with a minimal test case
2. Only after reproduction succeeds, plan the fix
3. After fixing, re-run the reproduction test to verify
```

This works because it changes the **plan structure** (3 exec tasks in a specific order instead of 1 "fix the bug" task).

## What Skills CANNOT Influence

Skills do NOT reach the execution layer:

- **Worker output**: the worker (shell command translator) has its own prompt and doesn't see skills
- **Tool behavior**: tools (aider, browser, websearch) run as separate processes — skill instructions don't reach them
- **Messenger style**: the messenger has its own prompt for formatting responses
- **Generated code quality**: scripts written by exec tasks are produced by the worker LLM, not the planner

### Example: Bad Skill (won't work)

```markdown
---
name: code-quality
summary: Write clean, well-documented Python code
---

When writing Python code:
- Use type hints on all function signatures
- Add docstrings to public functions
- Follow PEP 8 naming conventions
```

This **won't work** because the planner doesn't write code. It creates an exec task like `"Write a Python script to do X"`, and the **worker** LLM translates that into a shell command. The worker never sees this skill.

## Key Principle

> Skills shape the PLAN, not the EXECUTION.
> The planner decides **what** to do; the worker and tools decide **how** to do it.

## Managing Skills

```bash
kiso skill install /path/to/skill.md    # copy to ~/.kiso/skills/
kiso skill list                          # list installed skills
kiso skill remove <name>                 # remove a skill
```

## When to Create a Skill

Create a skill when you find yourself repeatedly telling kiso to follow a specific workflow. For example, if you always say "search first, then verify, then report", a skill encodes that pattern so the planner follows it automatically.

Do NOT create a skill for:
- Code style preferences (use aider config or `.editorconfig` instead)
- Response formatting (use behaviors: `kiso behavior add "..."`)
- Tool-specific settings (configure the tool itself)
