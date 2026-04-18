# Default preset

`kiso init --preset default` writes a `config.toml` with nine MCP
servers already wired up. A fresh install with only
`OPENROUTER_API_KEY` in the environment is fully functional —
every capability on the Claude Code / openclaw parity checklist
is covered.

The preset file lives in the repo at
[`kiso/presets/default.mcp.json`](../kiso/presets/default.mcp.json)
and is subject to two CI-enforced rules (see
[`docs/standards.md`](standards.md)).

## The nine servers

| Name          | Tier | Command | Upstream                                      | Key                       |
|---------------|------|---------|-----------------------------------------------|---------------------------|
| `filesystem`  | 1    | `npx`   | `@modelcontextprotocol/server-filesystem`     | (none)                    |
| `memory`      | 1    | `npx`   | `@modelcontextprotocol/server-memory`         | (none)                    |
| `browser`     | 1    | `npx`   | `@playwright/mcp` (Microsoft)                 | (none)                    |
| `github`      | 1    | `npx`   | `@modelcontextprotocol/server-github`         | `GITHUB_TOKEN` (optional) |
| `aider`       | 2    | `uvx`   | `kiso-run/aider-mcp`                          | `OPENROUTER_API_KEY`      |
| `search`      | 2    | `uvx`   | `kiso-run/search-mcp` (Perplexity Sonar)      | `OPENROUTER_API_KEY`      |
| `transcriber` | 2    | `uvx`   | `kiso-run/transcriber-mcp` (Gemini)           | `OPENROUTER_API_KEY`      |
| `ocr`         | 2    | `uvx`   | `kiso-run/ocr-mcp` (Gemini)                   | `OPENROUTER_API_KEY`      |
| `docreader`   | 2    | `uvx`   | `kiso-run/docreader-mcp` (pure Python)        | (none)                    |

Tier 1 = Anthropic / Microsoft / GitHub upstream. Tier 2 =
`kiso-run` upstream, same maintenance cadence as `kiso/core`.

## Per-server details

### `filesystem`

Reads and writes files under `${HOME}` by default. The path arg is
hardcoded to `${env:HOME}` in the preset; edit
`config.toml → [mcp.filesystem]` to restrict the accessible root.
Per-session workspace scoping (so each kiso session sees only its
own workspace) lands with M1512.

### `memory`

Anthropic's knowledge-graph MCP server. Persistent memory store
for long-running sessions; the planner can write to it and recall
across conversations.

### `browser`

`@playwright/mcp`, Microsoft-maintained. Replaces the retired
`browser` wrapper. Handles headless Playwright-backed page
navigation, screenshot capture, form interaction.

### `github`

GitHub repo / PR / issue operations via
`@modelcontextprotocol/server-github`. The preset injects
`GITHUB_TOKEN` into the server env; if the env var isn't set,
authenticated operations fail at call time (the server still
starts).

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
| `GITHUB_TOKEN`       | no       | Authenticated ops on the `github` server     |

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
uvx --from git+https://github.com/kiso-run/aider-mcp@v0.1.0 kiso-aider-mcp

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
