# Kiso Documentation

Welcome. This is the map for reading the Kiso docs tree.

Kiso is a general-purpose agent runtime. The docs are grouped by
concern: **what the product is**, **how to use it**, and **how it
works inside**. A new user should read them roughly in the order
below.

## Start here

1. [README.md](../README.md) — product overview, first install,
   minimum config to run.
2. [standards.md](standards.md) — the two standards Kiso is built
   on: Agent Skills and MCP. Understanding the two primitives
   comes before everything else.
3. [architecture.md](architecture.md) — the runtime at a glance:
   intake, planning, execution, review, replan, memory, delivery.
4. [flow.md](flow.md) — end-to-end lifecycle of a single message
   with every phase spelled out.

## Extending Kiso (the two primitives)

5. [skills.md](skills.md) — Agent Skills: how to author, install,
   and scope a skill; role sections; trust tiers.
6. [mcp.md](mcp.md) — MCP clients: install from URL, per-session
   pools, schema validation, Resources / Prompts / Sampling,
   recovery. The "single-key via OpenRouter" model is explained
   here.
7. [default-preset.md](default-preset.md) — what ships in
   `kiso init --preset default` and why.
8. [recommended-skills.md](recommended-skills.md) — curated
   non-preset skills with provenance disclaimers.
9. [recommended-mcps.md](recommended-mcps.md) — curated
   non-preset MCP servers with provenance disclaimers.
10. [extensibility.md](extensibility.md) — decision tree:
    skill vs. MCP vs. exec for a given capability gap.

## Operating Kiso

11. [cli.md](cli.md) — every `kiso` subcommand with examples.
12. [config.md](config.md) — `config.toml` schema, settings
    reference, model roles, users, providers.
13. [api.md](api.md) — HTTP API surface for connectors and the CLI.
14. [docker.md](docker.md) — multi-instance deployment, the
    `kiso` wrapper CLI, port allocation.
15. [hooks.md](hooks.md) — pre/post exec hooks for custom
    validation.

## Security and safety

16. [security.md](security.md) — the complete security model:
    roles, sandboxing, input validation, secret scoping, trust
    store.
17. [safety.md](safety.md) — the user-facing view of what Kiso
    will and will not do.
18. [security-risks.md](security-risks.md) — residual risks the
    runtime does not eliminate, documented honestly.
19. [audit.md](audit.md) — what the audit log records and where.

## How the LLM layer is organized

20. [llm-roles.md](llm-roles.md) — every LLM role (briefer,
    classifier, planner, reviewer, worker, messenger, summarizer,
    curator, consolidator, paraphraser, mcp_sampling), its
    prompt, and its default model.
21. [model-selection.md](model-selection.md) — guidance for
    choosing a model for each role.

## Internals

22. [database.md](database.md) — SQLite schema and runtime-state
    mapping.
23. [logging.md](logging.md) — log levels and where to find them.
24. [https.md](https.md) — TLS / reverse-proxy setup.
25. [testing.md](testing.md) — test suite layout and coverage
    strategy.
26. [testing-live.md](testing-live.md) — live-LLM tests and when
    to run them.
27. [devplan-format.md](devplan-format.md) — the format of
    `devplan/vN.md` files used to track milestones.

## Notes on coverage

All references in these docs reflect the v0.10 runtime. Legacy
concepts retired during the v0.10 cycle (the `wrapper` task type,
the `search` task type, the `searcher` LLM role, recipes as a
planner-only primitive, the connector plugin subsystem, the idea
of a Kiso-maintained registry) appear only where a historical
note is needed for context. They are never taught as current
usage.
