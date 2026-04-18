# Standards and Kiso runtime policy

Kiso builds on two external standards and layers a small **Kiso
runtime policy** on top. This document draws the line between what
is standard and what is Kiso-specific, so contributors know
exactly where to push back on Kiso conventions vs. where a change
would require upstream work.

## Agent Skills standard — [agentskills.io](https://agentskills.io)

Kiso adopts Agent Skills as its **instruction primitive**. A skill
is a directory (or single file) containing a `SKILL.md` with YAML
frontmatter and a Markdown body, optionally bundling `scripts/`,
`references/`, and other assets.

**Kiso follows the standard for:**

- Skill package layout (`SKILL.md` + optional bundled assets)
- Standard frontmatter fields: `name`, `description`, `license`,
  `compatibility`, `metadata`, `allowed-tools`
- Naming convention: lowercase, hyphens, 1–64 characters

**Kiso adds on top (the *Kiso Skill Profile*):**

- Extension frontmatter fields — `when_to_use`, `audiences`,
  `activation_hints`, `version`
- Role-scoped body sections: `## Planner`, `## Worker`,
  `## Reviewer`, `## Messenger` (opt-in; absent sections default
  to planner-only, matching recipe behavior)
- Deterministic `activation_hints` pre-filter that scales the
  catalog without embeddings

The Kiso profile is a **backward-compatible convention**: a
standards-compliant `SKILL.md` from any source works in kiso
without modification. The profile just gives each role a cleaner
view when the skill opts into it.

**Reference**: [`docs/skills.md`](skills.md) covers the kiso-facing
authoring and install UX.

## MCP standard — [modelcontextprotocol.io](https://modelcontextprotocol.io)

Kiso adopts MCP as its **capability surface**. Every user-facing
capability (codegen, search, transcription, filesystem, etc.)
comes from an MCP server; there is no privileged in-process tool
layer in kiso/core.

**Kiso follows the standard for:**

- Client/server protocol (JSON-RPC 2.0 over the MCP envelope)
- Lifecycle handshake (`initialize` / `initialized`)
- Transports — stdio and Streamable HTTP (2025-06-18)
- Tools, Resources, and Prompts primitives
- Tool `description` and `inputSchema` semantics

**Kiso adds on top (the *Kiso MCP Runtime Policy*):**

- Per-session client pool so concurrent sessions never share state
  (M1512)
- Scoped secret delivery through `~/.kiso/mcp/<name>.env` with
  strict 0600 permissions
- Install, test, trust, and health CLI: `kiso mcp
  add/install/test/env/logs/trust`
- Provenance tracking at `~/.kiso/mcp/<name>.provenance.json`
- The trust store at `~/.kiso/trust.json` (user-editable Tier 2+
  entries plus hardcoded Tier 1 prefixes)
- Cold-start and recovery semantics — `x-kiso-consumes` extension
  and the `mcp_recovery` planner module
- **Single-key invariant** for the default preset (see below)

These runtime policies are **not** part of the standard — they
exist because the MCP spec intentionally stays narrow and leaves
installation, per-session isolation, and trust as the host's
responsibility.

## Single-key invariant

The shipped default preset (see
[`docs/default-preset.md`](default-preset.md)) runs on **one**
mandatory key: `OPENROUTER_API_KEY`. Any kiso-maintained MCP in
the default that needs an LLM or search backend routes through
OpenRouter.

- `GITHUB_TOKEN` is the only *optional* extra (unlocks the
  `github` MCP server).
- The invariant is enforced by the CI test
  `tests/test_presets_default.py::TestSingleKeyRule`.

## Trust tiering

The preset distinguishes:

- **Tier 1** — organizations with public release pipelines:
  Anthropic (`@modelcontextprotocol/*`), Microsoft
  (`@playwright/*`), GitHub (`@github/*`).
- **Tier 2** — `kiso-run/*-mcp` servers maintained by the same
  team as `kiso/core`.

Community servers and single-developer projects are listed in
[`docs/recommended-mcps.md`](recommended-mcps.md) and
[`docs/recommended-skills.md`](recommended-skills.md), not shipped
in the default.

## Contributing a compatible skill or MCP server

**For skills** — author a standards-compliant `SKILL.md`, publish
in a GitHub repo or zip, and point users at
`kiso skill install --from-url <url>`. If you want the skill to
benefit from role-scoped projection, add the Kiso extension
fields (`when_to_use`, `audiences`, `activation_hints`) and role
sections. See [`docs/skills.md`](skills.md) for details.

**For MCP servers** — use the official SDK in any supported
language, declare tools/resources/prompts per the spec, and
publish under your own npm / pypi / git coordinates. Users
register it with `kiso mcp install --from-url <url>` — no default
preset inclusion required. For inclusion in
`docs/recommended-mcps.md`, submit a PR with an
`upstream`/`license`/`key-requirements` block following the
conventions in that file.
