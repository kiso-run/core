# Default preset

`kiso init --preset default` writes a `config.toml` with seven MCP
servers already wired up. A fresh install with only
`OPENROUTER_API_KEY` in the environment is fully functional —
every general-purpose capability on the Claude Code / openclaw
parity checklist is covered.

The preset file lives in the repo at
[`kiso/presets/default.mcp.json`](../kiso/presets/default.mcp.json)
and is subject to two CI-enforced rules (see
[`docs/standards.md`](standards.md)).

## The seven servers

| Name          | Tier | Command | Upstream                                      | Key                       |
|---------------|------|---------|-----------------------------------------------|---------------------------|
| `filesystem`  | 1    | `npx`   | `@modelcontextprotocol/server-filesystem`     | (none)                    |
| `browser`     | 1    | `npx`   | `@playwright/mcp` (Microsoft)                 | (none)                    |
| `aider`       | 2    | `uvx`   | `kiso-run/aider-mcp`                          | `OPENROUTER_API_KEY`      |
| `search`      | 2    | `uvx`   | `kiso-run/search-mcp` (Perplexity Sonar)      | `OPENROUTER_API_KEY`      |
| `transcriber` | 2    | `uvx`   | `kiso-run/transcriber-mcp` (Gemini)           | `OPENROUTER_API_KEY`      |
| `ocr`         | 2    | `uvx`   | `kiso-run/ocr-mcp` (Gemini)                   | `OPENROUTER_API_KEY`      |
| `docreader`   | 2    | `uvx`   | `kiso-run/docreader-mcp` (pure Python)        | (none)                    |

Tier 1 = Anthropic / Microsoft upstream. Tier 2 = `kiso-run`
upstream, same maintenance cadence as `kiso/core`.

## Servers explicitly NOT in the default

- **`memory`** (`@modelcontextprotocol/server-memory`) — would
  duplicate Kiso's own knowledge pipeline (`facts` table with
  FTS5 search, `learnings` queue, curator role with
  promote/ask/discard verdicts, decay + archival, scoping per
  session/project/user, behaviour injection). The npm memory MCP
  is a flat JSONL graph with no scoping, no curation, no decay,
  no FTS — strictly less than what Kiso already does. Two stores
  in parallel would be worse than either alone (the planner
  would not know which one to write to). Install on demand only
  if you really need an external knowledge graph that other MCP
  clients can also read.
- **`github`** — specialised, opt-in. PR/issue/commit on a
  specific repo is not a general-purpose capability and the
  trust gate around `GITHUB_TOKEN` is non-trivial. Install
  on demand:
  `kiso mcp install --from-url npm:@modelcontextprotocol/server-github`.

## Per-server details

### `filesystem`

Reads and writes files under `${HOME}` by default. The path arg is
hardcoded to `${env:HOME}` in the preset; edit
`config.toml → [mcp.filesystem]` to restrict the accessible root.
For per-session workspace isolation, replace `${env:HOME}` with
`${session:workspace}` — kiso then spawns a separate subprocess
per session, each rooted in its own workspace directory. See
`docs/mcp.md` under *Per-session client pool*.

### `browser`

`@playwright/mcp`, Microsoft-maintained. Replaces the retired
`browser` wrapper. Handles headless Playwright-backed page
navigation, screenshot capture, form interaction.

### `aider`

Codegen via aider, routed through OpenRouter. Replaces the
retired `aider` wrapper. Tools: `aider_codegen` (edit files,
return `git diff`) and `doctor`.

### `search`

Web search via Perplexity Sonar, routed through OpenRouter. Sole
MCP option in the default that satisfies both the single-key rule
and the trust rule. Tools: `web_search` and `doctor`.

### `transcriber`

Audio transcription via Gemini 2.5 Flash Lite, routed through
OpenRouter. Auto-compresses to OGG Opus before transfer. Tools:
`transcribe_audio`, `audio_info`, `doctor`.

### `ocr`

Image OCR and description via Gemini 2.0 Flash, routed through
OpenRouter. Tools: `ocr_image`, `describe_image`, `image_info`,
`doctor`.

### `docreader`

Pure-Python document extraction (PDF, DOCX, XLSX, CSV, plain text)
via `pypdf` / `python-docx` / `openpyxl`. No key required. Tools:
`read_document`, `document_info`, `list_supported_formats`,
`doctor`.

## Required environment

| Variable             | Required | Unlocks                                      |
|----------------------|----------|----------------------------------------------|
| `OPENROUTER_API_KEY` | yes      | `aider`, `search`, `transcriber`, `ocr`      |

## Regenerating from scratch

```sh
kiso init --preset default --force
```

`--force` overwrites `~/.kiso/config.toml`; omit it to protect an
existing config.

## Minimal (empty MCP block)

```sh
kiso init --preset none
```

Writes the base config template with no `[mcp.*]` sections. Useful
for users who want to add servers manually via `kiso mcp
add/install --from-url`.

## Verifying the preset locally

Each MCP server in the preset is pinned to a specific release tag
(`v0.1.0` for the kiso-run Tier 2 servers; version-tagged npm
packages for Tier 1). You can sanity-check a server without
editing `config.toml`:

```sh
# Tier 2
uvx --from git+https://github.com/kiso-run/aider-mcp@v0.2.0 kiso-aider-mcp

# Tier 1
npx -y @modelcontextprotocol/server-filesystem@2026.1.14 ~/
```

Both should start, print a one-line stdio banner, and wait for
MCP client traffic on stdin. Send `Ctrl-D` to exit.

## Why not more servers

The default stays small on purpose. Every entry has to satisfy:

- trust rule (one of four approved command shapes)
- single-key rule (only `OPENROUTER_API_KEY` or `GITHUB_TOKEN`)
- covers a capability not already covered by another preset entry

Community servers that fail any of these live in
[`docs/recommended-mcps.md`](recommended-mcps.md) with install
instructions, not in the default.
