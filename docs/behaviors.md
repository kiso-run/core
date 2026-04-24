# Behaviors

A **behavior** is a persistent guideline that shapes how kiso writes
responses. Unlike a fact (which feeds the planner), a behavior is
injected directly into the **messenger** role's system prompt so
every user-facing reply honours it.

Behaviors are session-agnostic by default — they apply to every
session a user participates in — and ship as a first-class CLI
primitive. Rules (`kiso rules`) are the safety-oriented sibling:
hard constraints the planner must respect, never negotiable in
messenger output. Behaviors are softer style/voice preferences.

## Adding a behavior

```bash
kiso behavior add "always reply in markdown, even for one-line answers"
kiso behavior add "prefer metric units; list imperial in parentheses"
kiso behavior add "no bullet points unless the user asks"
```

Each entry is stored as a fact with `category = "behavior"` in
`store.facts`. The messenger receives all current behaviors in the
`## Behavior Guidelines (follow these preferences)` section of its
prompt.

## Listing behaviors

```bash
kiso behavior list
```

Each entry has an integer id for removal.

## Removing a behavior

```bash
kiso behavior remove 7
```

The id comes from `kiso behavior list`.

## How behaviors reach the messenger

At every `msg` task dispatch the worker builds the messenger prompt
via `kiso/brain/text_roles.py::build_messenger_messages`. The
builder queries behavior rows from `store.facts WHERE category =
'behavior'` and splices them into the system prompt as explicit
guidelines. There is no LLM selection step — every active behavior
always reaches every messenger call.

## Behaviors vs. rules vs. facts

| Concept | Lives in | Enforced by | Who applies it |
|---|---|---|---|
| **Behavior** (`kiso behavior`) | `facts.category = 'behavior'` | Messenger prompt injection | Messenger LLM |
| **Rule** (`kiso rules`) | `facts.category = 'safety'` | Planner + Reviewer prompt injection | Planner rejects violating plans; Reviewer flags failures |
| **Fact** (`kiso knowledge add`) | `facts.category` in `{project,user,tool,general}` | Briefer selects relevant ones | Planner (not messenger) |

Use a **behavior** for style / voice preferences.
Use a **rule** for hard constraints ("never delete `/data`").
Use a **fact** for domain knowledge kiso should remember.

## Behavior storage

Behaviors are global by default. Session scoping would require a
separate category and selection path, and is not part of v0.10. If
you need session-specific phrasing, include it in the session's
`description` when you `kiso session create` instead.

## API surface

Behaviors are exposed via the same admin endpoints that back
`kiso knowledge`:

- `GET /admin/knowledge?category=behavior` — list
- `POST /admin/knowledge` with `{"content": "...", "category":
  "behavior"}` — add
- `DELETE /admin/knowledge/<id>` — remove

See [api.md](api.md) for the full shape.

## See also

- [cli.md](cli.md) — `kiso behavior` subcommand reference
- [llm-roles.md](llm-roles.md#messenger) — messenger role and how
  it consumes behaviors
- [config.md](config.md) — no behavior-related settings; the
  subsystem has no runtime toggles
