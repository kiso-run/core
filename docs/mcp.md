# MCP — Model Context Protocol

Kiso is an MCP **client**. You can connect it to any server in the
MCP ecosystem and Kiso will expose the server's methods to the
planner as first-class task primitives, alongside wrappers and
recipes.

Kiso is **not** an MCP server publisher. We do not ship or curate
a registry of MCP servers. When you need one, you bring it from
the wider ecosystem.

> **Terminology note.** Throughout this document and the rest of
> Kiso's vocabulary, an MCP server exposes **methods**. The MCP
> specification itself uses the word *"tool"* for the same
> concept (cf. `tools/list`, `tools/call`). We use "method" to
> avoid confusion with Kiso's earlier "tool" terminology (now
> renamed to "wrapper"). When reading the MCP spec, mentally
> substitute *tool* → *method*.

## Finding servers

There is no kiso-curated list. Real places to look:

- [pulsemcp.com](https://www.pulsemcp.com) — community catalog with
  server pages, install snippets, and a standardized `server.json`
  endpoint per server
- [mcp.so](https://mcp.so) — another community catalog
- [github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)
  — the official reference implementations (filesystem, git,
  github, gitlab, memory, fetch, brave-search, postgres, and
  many more)
- `npm` / `pypi` / GitHub search — every MCP server eventually
  ends up in one of these

## Installing a server

Two paths: direct CLI form (if you know exactly what you want) and
URL resolver (recommended).

### URL-based install

```bash
kiso mcp install --from-url <url-or-identifier>
```

Supported forms:

- `https://www.pulsemcp.com/servers/<name>` — fetches the server's
  standardized `server.json` and normalises it
- `https://github.com/<owner>/<repo>` — clones the repo into
  `~/.kiso/mcp/servers/<name>/`, creates an isolated venv via
  `uv venv`, installs via `uv pip install -e .`
- `https://www.npmjs.com/package/<pkg>` — configures `npx -y <pkg>`
- `npm:<pkg>` (pseudo-URL) — same as above
- `https://pypi.org/project/<pkg>/` — configures `uvx <pkg>`
- `pypi:<pkg>` (pseudo-URL) — same as above
- Any URL whose path ends with `server.json` or `mcp.json` —
  fetches and normalises directly

Install policy: **ephemeral runners only**. Kiso never runs
`pip install` with the system Python and never runs
`npm install -g`. PyPI servers run via `uvx`, npm servers via
`npx -y`; git-clone servers get their own isolated venv under
`~/.kiso/mcp/servers/`.

**Runtime prerequisites**: `uv` and `npx` must be on `PATH`. Kiso
pre-flights these and emits clear install links for whichever is
missing:

- `uv` → [docs.astral.sh/uv](https://docs.astral.sh/uv/)
- `npx` → [nodejs.org](https://nodejs.org/) (ships with Node.js)

### Direct CLI form (advanced)

```bash
# Stdio server
kiso mcp add <name> stdio \
  --command <exec> \
  --args <arg1> <arg2>... \
  --env KEY=VAL... \
  --cwd <dir> \
  --timeout-s 60

# HTTP server
kiso mcp add <name> http \
  --url https://example.com/mcp \
  --header KEY=VAL... \
  --timeout-s 60
```

Direct form is validated through the same config parser as the
URL path: invalid transports, missing required fields, names
outside `NAME_RE`, and `KISO_*` env keys are all rejected at add
time.

## Three concrete examples

### 1. GitHub (npm, stdio, PAT auth) — canonical

```bash
kiso mcp install --from-url https://www.pulsemcp.com/servers/github
```

Writes to `~/.kiso/config.toml`:

```toml
[mcp.github]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
```

Then supply a GitHub Personal Access Token:

```bash
# Generate a PAT at https://github.com/settings/tokens
kiso mcp env github set GITHUB_PERSONAL_ACCESS_TOKEN <your-token>
```

The token lands in `~/.kiso/mcp/github.env` with `0600` perms
(owner-only read/write). Kiso never logs it and never passes it
through the conversation history.

Verify the setup:

```bash
kiso mcp test github
```

Expected output:

```
✓ initialize OK
✓ list_methods: 26 method(s)
  - github:create_issue  Create a new issue
  - github:list_issues   List issues in a repo
  ...
✓ shutdown OK
```

### 2. Google Maps Grounding Lite (remote HTTP, API key)

```bash
kiso mcp add google-maps http \
  --url https://mapstools.googleapis.com/mcp \
  --header "X-Goog-Api-Key=fake-placeholder"
```

Then supply the real key (generate one in Google Cloud Console):

```bash
kiso mcp env google-maps set GOOGLE_MAPS_API_KEY <your-key>
```

Note: for HTTP servers, headers in the config are static; dynamic
credential injection is done via env vars that the server reads
from its own startup. Google Maps documents its auth in the
[Grounding Lite docs](https://developers.google.com/maps/ai/grounding-lite).

### 3. Community Python server via pip-installed package

```bash
kiso mcp install --from-url pypi:some-mcp-server
```

Writes:

```toml
[mcp.some-mcp-server]
transport = "stdio"
command = "uvx"
args = ["some-mcp-server"]
```

`uvx` downloads and runs the package in an isolated venv on first
use. No global install, no cleanup needed.

## Credentials

The rule: **secrets go through `kiso mcp env`, never through
chat**.

```bash
kiso mcp env <server> set KEY VALUE    # set a credential
kiso mcp env <server> unset KEY        # remove one
kiso mcp env <server> list             # show keys only
kiso mcp env <server> show             # show keys AND values (WARNING: secrets visible)
```

Files live at `~/.kiso/mcp/<server>.env` with `0600` permissions.
They are merged into the MCP subprocess environment at spawn time,
layered on top of `os.environ` minus the `KISO_*` deny-list.

If a user pastes a secret in a chat message, Kiso's planner and
messenger refuse to store it and redirect the user to the CLI.
Secrets in chat end up in session history and briefing contexts,
where they can leak into logs and LLM prompts — the CLI path
avoids all of that.

### Servers that require OAuth

Some MCP servers (notably Google Workspace integrations) need an
OAuth consent step. Kiso does not drive OAuth itself — each
server handles auth its own way, documented in its README. The
common patterns:

1. **Static refresh token** (most common, most headless-friendly):
   you obtain a refresh token once from the provider, then set it
   as an env var via `kiso mcp env`. Works in any deployment.
2. **OAuth Device Flow (RFC 8628)**: the server prints a URL + code
   on startup; you complete the consent from any device; the server
   polls for the token and persists it. Works for some servers,
   depends on provider support.
3. **OAuth Authorization Code + localhost callback** (e.g.
   `acamolese/google-search-console-mcp`): requires the user to
   have shell + browser access on the same machine the server
   runs on. **Not headless-friendly**. Works for self-hosted
   single-machine setups; fails for deployments where Kiso runs
   on a remote server.

When a server falls into category 3 and you're running Kiso on a
machine without browser access, that specific server is not
usable in your deployment. Look for a static-token alternative
or ask the server author to add device-flow support.

## Security

MCP servers run with access to whatever you give them. Review the
set of methods exposed by each server before approving tasks
that call them. In particular:

- File system servers can read/write anywhere they have OS
  permissions. Restrict via `cwd` in the config when possible.
- API servers can take any action the provided token authorizes.
  Generate tokens with the least privileges needed.
- HTTP servers are remote; they see whatever data you pass them
  in tool arguments. Treat them as external untrusted services.

Kiso declares **no optional client capabilities** on the
`initialize` handshake: no `roots`, no `sampling`, no
`elicitation`. Servers that depend on these will degrade
gracefully per spec; if you need one of them, please open an
issue describing the use case.

## Troubleshooting

```bash
kiso mcp test <server>           # re-run the handshake + list
kiso mcp logs <server>           # tail the stderr log
kiso mcp logs <server> --tail 200
kiso mcp list                    # show all configured servers
```

Common errors:

- `initialize failed: command not found` — the configured
  `command` is not on PATH. Check with `which <command>` from a
  shell.
- `405 Method Not Allowed` (HTTP) — the server only speaks the
  deprecated HTTP+SSE transport. Kiso supports only the current
  Streamable HTTP transport. If the server offers a stdio mode,
  use that instead.
- `session expired` (HTTP) — transparent; Kiso re-initializes
  automatically on 404. If it loops, the server has a bug.
- `KISO_ may not set KISO_* variables` — you tried to set a
  `KISO_*` env var in a server config. Those are reserved;
  rename your variable.

Log files live at `~/.kiso/mcp/<server>.err.log` (appended per
session). The tail is ring-buffered in memory per-server (1 MB
by default) so a chatty server cannot fill your disk.

## Capabilities not currently supported

Kiso v0.9 implements the MCP **tools** primitive only. The
following spec features are not yet implemented:

- `resources` (server-side passive content)
- `prompts` (server-side prompt templates)
- `roots` (client-declared filesystem roots)
- `sampling` (server can call the client's LLM)
- `elicitation` (server prompts the user via client UI)
- `logging` (server-side structured logs)
- OAuth device flow inside the client

Open an issue if you need one of these for a concrete use case.

## Community examples for retired wrappers

Kiso v0.9 retired three wrappers that violated the boundary rule
(pure remote-API proxies with no local-install lifecycle). Users
who still want that functionality can configure community MCP
servers from the ecosystem:

| Retired wrapper | Community MCP alternatives (not endorsed) |
|---|---|
| `gworkspace` | Search pulsemcp.com and `modelcontextprotocol/servers` for "google" — several Google Drive / Calendar / Docs MCPs exist |
| `websearch` | `@modelcontextprotocol/server-brave-search`, Google Maps Grounding Lite for geo-aware grounding, or the built-in `search` task type (no MCP needed for basic web search) |
| `moltbook` | `thebenlamm/moltbook-mcp` (pip-installable Python server) |

These are community servers maintained by third parties. Kiso
has no SLA or security review for any of them. Read the server's
README and consider the trust implications before configuring.

## Disclaimer

Everything in the MCP ecosystem is community-maintained unless
explicitly noted. Kiso connects to what you configure; it does
not vet the servers you point it at. Review the tools exposed by
each server and the credentials you provide before running plans
that call them.
