# Recommended MCP servers (outside the default preset)

Community MCP servers that are useful but do **not** ship in the
[default preset](default-preset.md). Each entry fails at least
one of the default's invariants (single-key, trust-tier, or
collision-risk) — that's why it lives here.

Listing here is **not** an endorsement of upstream quality. Do
your own provenance check before `kiso mcp install --from-url`.

## Format

Each entry has:

- **Upstream** — where the code lives
- **License**
- **Key requirements** — env vars the server needs
- **Trust flag** — which default-preset rule it misses
- **Install hint** — the `kiso mcp install --from-url` command

---

## Perplexity (official)

- **Upstream**: [`perplexity-ai/modelcontextprotocol`](https://github.com/perplexity-ai/modelcontextprotocol)
- **License**: MIT
- **Key**: `PERPLEXITY_API_KEY`
- **Trust flag**: fails **single-key** — requires a separate
  Perplexity API key instead of routing through OpenRouter. Use
  `kiso-run/search-mcp` (in the default) unless you specifically
  need Perplexity-direct.
- **Install**: `kiso mcp install --from-url https://github.com/perplexity-ai/modelcontextprotocol`

## Brave Search

- **Upstream**: [`@modelcontextprotocol/server-brave-search`](https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search)
- **License**: MIT
- **Key**: `BRAVE_API_KEY`
- **Trust flag**: fails **single-key**.
- **Install**: `kiso mcp install --from-url npm:@modelcontextprotocol/server-brave-search`

## Sequential Thinking

- **Upstream**: [`@modelcontextprotocol/server-sequential-thinking`](https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking)
- **License**: MIT
- **Key**: none
- **Trust flag**: passes trust + single-key. Not in default
  because the planner's own loop already does multi-step
  reasoning; shipping both creates routing ambiguity. Add it if
  your use case is heavily reasoning-bound and you want an
  externalized scratchpad.
- **Install**: `kiso mcp install --from-url npm:@modelcontextprotocol/server-sequential-thinking`

## Slack

- **Upstream**: various community forks; no single blessed
  upstream at the time of this writing.
- **Trust flag**: fails **trust** — no Tier 1 upstream (Slack has
  not published an official MCP server).
- **Key**: `SLACK_BOT_TOKEN` (also fails single-key).
- **Install**: investigate forks via the MCP registries below.

## Fetch (Python)

- **Upstream**: [`modelcontextprotocol/servers`](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)
- **License**: MIT
- **Trust flag**: Python-only (not on npm), so the trust rule's
  `uvx --from git+https://github.com/kiso-run/*-mcp@*` pattern
  doesn't accept it — kiso-run doesn't mirror it. The `browser`
  preset entry covers HTTP retrieval.
- **Install**: manual — use `uvx --from git+https://github.com/modelcontextprotocol/servers#subdirectory=src/fetch mcp-server-fetch`.

## Git

- **Upstream**: [`modelcontextprotocol/servers/src/git`](https://github.com/modelcontextprotocol/servers/tree/main/src/git)
- **License**: MIT
- **Trust flag**: Python-only; same shape as `fetch`.
- **Install**: `uvx --from git+https://github.com/modelcontextprotocol/servers#subdirectory=src/git mcp-server-git`

## Postgres / SQLite

- **Upstream**: [`modelcontextprotocol/servers`](https://github.com/modelcontextprotocol/servers)
- **License**: MIT
- **Key**: DB connection string per server
- **Trust flag**: passes trust (npm-published by Anthropic) but
  specialised capability — not in default to keep the 9-server
  list focused on general-purpose use. Add per-project.
- **Install**: `kiso mcp install --from-url npm:@modelcontextprotocol/server-postgres` (or `-sqlite`)

---

## Where to discover more

- [mcp.so](https://mcp.so) — community index
- [glama.ai/mcp](https://glama.ai/mcp) — discovery site
- [smithery.ai](https://smithery.ai) — registry with hosted installs
- [GitHub topic: `modelcontextprotocol`](https://github.com/topics/modelcontextprotocol)

**Safety reminder**: community MCP servers can run arbitrary code
in your environment. `kiso mcp trust` lets you allowlist
prefixes, but the first time you install from a new upstream,
read the source.
