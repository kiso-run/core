# Extending Kiso

Kiso has three extension surfaces. Pick one based on what your
extension actually does.

## Decision tree

### 1. Wrapper

Use a wrapper **only when all three** of the following are true:

- Your extension installs software on the host machine (binaries,
  models, system packages)
- You need to manage its lifecycle — install, upgrade, uninstall,
  deps check
- You want deep integration with the session workspace (read and
  write local files, publish artifacts via the `Published files:`
  pipeline)

Good examples shipped with Kiso:

- `aider` — installs the aider binary via `uv`, operates on repo
  files, publishes diffs
- `browser` — installs Playwright + Chromium, takes screenshots,
  publishes them
- `ocr` — installs Tesseract + language packs, reads image files,
  emits text
- `transcriber` — installs whisper models, reads audio files, emits
  text
- `docreader` — installs parsing libs, reads PDF/docx files, emits
  text

Wrappers live in separate repos under `kiso-run/` (e.g.
`kiso-run/wrapper-aider`). See `docs/wrapper-dev.md` for the
authoring guide.

### 2. MCP server

Use an MCP server (or point users at one that already exists) when
your extension is a **remote-API proxy** with:

- An external service (GitHub, GitLab, Slack, Google APIs,
  Anthropic…) — auth via tokens or OAuth setup handled by the
  server itself
- No local-install lifecycle beyond a thin client package (npm,
  PyPI) or a subprocess launched from a cloned repo
- No session workspace integration (or only at the consumer end,
  via standard MCP resource types)

**Kiso is a consumer of the MCP ecosystem**, not a publisher. We
don't ship our own MCP servers and we don't maintain a curated
registry of them. Users bring their own from the wider ecosystem
(pulsemcp.com, mcp.so, `modelcontextprotocol/servers`, npm, PyPI,
GitHub). When you ask Kiso to install an MCP server without a
concrete URL, Kiso asks for the URL — it will not guess.

See `docs/mcp.md` for the full user guide including three concrete
example configurations and the auth setup patterns.

### 3. Recipe

Use a recipe when your extension is **just a set of reusable
planner instructions** — no new code, no new dependencies, no new
runtime surface. Recipes live in `~/.kiso/recipes/*.md` and are
loaded into the planner context at plan time.

See `docs/recipes.md` for the recipe format and examples.

## The boundary rule

The single question that decides between wrapper and MCP:

> Does the extension install software on the host machine and
> require lifecycle management?

- **Yes** → wrapper
- **No** (it just proxies a remote API) → MCP server

This is why v0.9 retired three wrappers that violated the rule.
`gworkspace`, `websearch`, and `moltbook` each made `httpx` calls
to a remote API and installed nothing locally. Each is now
represented in the ecosystem as an MCP server that users can
configure explicitly (see `docs/mcp.md` for recommended community
examples).

## Summary

| Surface | Install software? | Remote API? | Workspace files? | Typical source |
|---|---|---|---|---|
| Wrapper | yes | optional | yes | `kiso-run/wrapper-*` repo |
| MCP server | no (client is thin) | yes | via MCP resources | community registry |
| Recipe | no | no | no | local markdown file |

When in doubt, start with MCP: it's the lowest-commitment surface
that still gives you real capability. Only reach for a wrapper
when you genuinely need install + manage semantics on the host.
