# Extending Kiso

Kiso has three extension surfaces. Pick one based on what your
extension actually does.

## Decision tree

### 1. MCP server

Use an MCP server when your extension provides **tools, resources,
or prompts** that the assistant should be able to call at runtime.

MCP (Model Context Protocol) is the primary integration surface
for capabilities in v0.10. Anything that used to be a "wrapper" —
code editing, browser automation, OCR, transcription, document
reading, search — is an MCP server in v0.10. Kiso is a consumer of
the MCP ecosystem: it does not ship its own servers as Kiso-only
artefacts and does not maintain a curated registry.

Good examples shipped with the default preset:

- `@playwright/mcp` — headless browser automation
- `kiso-run/aider-mcp` — code editing / refactor via the
  aider architect+editor pattern
- `kiso-run/search-mcp` — Perplexity Sonar search
- `kiso-run/ocr-mcp` — image OCR via Gemini vision
- `kiso-run/transcriber-mcp` — audio transcription via Gemini
- `kiso-run/docreader-mcp` — PDF / DOCX / CSV text extraction

Install an MCP server from a concrete URL:

```
kiso mcp install --from-url \
    uvx --from git+https://github.com/kiso-run/aider-mcp@v0.1.0 kiso-aider-mcp
```

Or bring your own from the community — pulsemcp.com, mcp.so,
`modelcontextprotocol/servers`, npm, PyPI, GitHub. Kiso never
guesses a URL: if you ask it to install "a search MCP" without a
URL, it asks you for one.

See `docs/mcp.md` for the full install / auth / session-scoped env
guide.

### 2. Skill

Use a skill when your extension is **role-scoped prompt guidance**
for the LLM — no new code, no new dependencies. Skills live in
`~/.kiso/skills/<name>/SKILL.md` and are projected into the
planner / worker / reviewer / messenger / curator / briefer
prompts according to their front-matter.

A `SKILL.md` has YAML front-matter declaring `name`, `summary`,
optional `activation_hints`, and role-scoped sections
(`## Planner`, `## Worker`, `## Reviewer`, …) that are pulled into
the matching role's system prompt.

Install a skill from a URL:

```
kiso skill install --from-url https://github.com/org/my-skill
```

See `docs/skills.md` for the SKILL.md format (docs/skills.md will
be filled in during the pre-release docs sweep).

### 3. Connector

Use a connector when your extension is a **messaging channel** —
Discord, Slack, Matrix, WhatsApp, a webhook. Connectors bridge an
external chat surface to a Kiso session.

Connectors are installable plugins under `~/.kiso/connectors/`
today. See `docs/connectors.md` for the current authoring model;
the model moves to a standalone-package supervisor-config shape
later in v0.10 (see the active devplan).

## The boundary rule

- Need to call out to an external API or local tool at runtime?
  → **MCP server**.
- Need to push prompt guidance into a specific role without
  shipping code? → **Skill**.
- Need to bridge a chat surface (Discord, Slack, …) to Kiso?
  → **Connector**.

## Summary

| Surface    | Shape                          | Config location                         | Install                                    |
|------------|--------------------------------|-----------------------------------------|--------------------------------------------|
| MCP server | stdio / HTTP process           | `~/.kiso/mcp/<name>.json` + `.env`      | `kiso mcp install --from-url <url>`        |
| Skill      | `SKILL.md` with role sections  | `~/.kiso/skills/<name>/`                | `kiso skill install --from-url <url>`      |
| Connector  | installable Python package     | `~/.kiso/connectors/<name>/`            | `kiso connector install <url-or-name>`     |

When in doubt, start with an MCP server: it is the lowest-
commitment surface that still gives the assistant real capability,
and the widest community ecosystem.

See also:

- `docs/mcp.md` — MCP install and runtime
- `docs/connectors.md` — connector authoring and deployment
- `docs/standards.md` — trust tiers and install-from-URL safety
