# Wrappers

A wrapper is a git-cloned package in `~/.kiso/instances/{instance}/wrappers/{name}/` on the host (mounted at `KISO_DIR/wrappers/{name}/` inside the container). Runs as a subprocess in a `uv`-managed venv.


## Structure

```
~/.kiso/instances/{instance}/wrappers/    # host path
KISO_DIR/wrappers/                     # container-internal path (equivalent)
├── search/
│   ├── kiso.toml           # manifest (required) — identity, args schema, deps
│   ├── pyproject.toml      # python dependencies (required, uv-managed)
│   ├── run.py              # entry point (required)
│   ├── deps.sh             # system deps installer (optional, idempotent)
│   ├── README.md           # docs for humans (optional)
│   └── .venv/              # created by uv on install
└── .../
```

A directory is a valid wrapper if it contains `kiso.toml` (with `type = "wrapper"`), `pyproject.toml`, and `run.py`.

All three are required — install fails if any is missing.

## kiso.toml

The single source of truth. Declares what this wrapper is, what arguments it takes, what it needs, and what system deps it requires.

```toml
[kiso]
type = "wrapper"
name = "search"
version = "0.1.0"
description = "Web search using Brave Search API"

[kiso.wrapper]
summary = "Web search using Brave Search API"    # one-liner for the planner
# session_secrets = ["github_token"]             # user-provided credentials (not needed for this wrapper)
# usage_guide = "Use short queries. Prefer English keywords."  # operational guidance for the planner

[kiso.wrapper.args]
query = { type = "string", required = true, description = "search query" }
max_results = { type = "int", required = false, default = 5, description = "number of results to return" }

[kiso.wrapper.env]
api_key = { required = true }     # → KISO_TOOL_SEARCH_API_KEY

[kiso.deps]
python = ">=3.11"
bin = ["curl"]                    # checked with `which` after install
```

### consumes

Optional field declaring what file types this wrapper can process. Used to
auto-generate a "File processing" routing section in the planner context,
so the planner knows which wrapper to use for which file type.

```toml
[kiso.wrapper]
summary = "Image OCR — extract text from photos, screenshots, receipts"
consumes = ["image"]
```

Valid vocabulary: `image`, `document`, `audio`, `video`, `code`, `web_page`.
Unknown values are warned and skipped (forward-compatible).

When at least one installed wrapper declares `consumes`, the planner sees:

```
File processing (match session workspace files to these wrappers):
- image files → ocr (Image OCR), describe (Describe image contents)
- document files → docreader (Read documents)
- audio files → transcriber (Transcribe audio)
```

### Two Kinds of Secrets

- `[kiso.wrapper.env]` → **deploy secrets** (env vars, set by admin via `kiso env`, passed via subprocess environment)
- `session_secrets` → **ephemeral secrets** (user-provided at runtime, in worker memory only — never persisted. Passed via input JSON — only declared ones)

See [security.md — Secrets](security.md#5-secrets) for the full comparison and scoping rules.

### Usage Guide

The `usage_guide` field in `[kiso.wrapper]` provides operational guidance visible
to the planner. On install, this text is copied to `usage_guide.local.md` in
the wrapper directory. Edit that file to customize how the agent uses the wrapper.

The local file is git-ignored — `kiso wrapper update` won't overwrite your edits.

Example:

```toml
[kiso.wrapper]
summary = "Web search using Brave Search API"
usage_guide = """\
Use short, specific queries. Prefer English keywords.
For code searches, include the language name.
max_results=3 is usually enough."""
```

### What the Planner Sees

The planner receives the one-liner, the args schema, and the usage guide (if set):

```
Available wrappers:
- search — Web search using Brave Search API
  args: query (string, required): search query
        max_results (int, optional, default=5): number of results to return
  guide: Use short, specific queries. Prefer English keywords.
```

This is enough for the planner to generate correct invocations. No ambiguity.

### Env Var Naming

Env vars follow the convention `KISO_TOOL_{NAME}_{KEY}`, built automatically:

| Manifest key | Env var |
|---|---|
| `api_key` | `KISO_TOOL_SEARCH_API_KEY` |
| `token` | `KISO_TOOL_SEARCH_TOKEN` |

Name and key are uppercased, `-` becomes `_`.


### Version

Display metadata. Shown in `kiso wrapper list` output. Kiso does not enforce version compatibility — updates are always `git pull` to latest.

## run.py

Reads JSON from stdin, writes result to stdout.

```python
import json
import sys


def run(args, context):
    """
    args:    arguments passed by the planner (dict, parsed from JSON string)
    context: full input dict (includes args, session, workspace, session_secrets)
    return:  result text (str)
    """
    # wrapper logic here
    return "result"


if __name__ == "__main__":
    data = json.load(sys.stdin)
    result = run(data["args"], data)
    print(result)
```

No async, no imports from kiso, no shared state. JSON in, text out.

### Input (stdin)

```json
{
  "args": {"query": "python async patterns", "max_results": 5},
  "session": "dev-backend",
  "workspace": "/home/user/.kiso/sessions/dev-backend",
  "session_secrets": {"github_token": "ghp_abc123"},
  "plan_outputs": [
    {"index": 1, "type": "exec", "detail": "ls src/", "output": "main.py\nutils.py", "status": "done"}
  ]
}
```

- `session_secrets`: **only** the keys declared in `kiso.toml`, not the full session credentials.
- `plan_outputs`: outputs from preceding tasks in the same plan. See [flow.md — Task Output Chaining](flow.md#task-output-chaining). Empty array if this is the first task. Wrappers can use it or ignore it.
- `workspace`: the session directory. Always contains two subdirectories:
  - `pub/` — write files here to make them publicly accessible via a URL (see [flow.md — Public File Serving](flow.md#public-file-serving))
  - `uploads/` — files received from the outside (email attachments, Discord files, etc.) are written here by connectors; wrappers can read from it

### Output (stdout)

Plain text. Everything on stdout becomes the task output. Stderr is captured separately for debugging.

## deps.sh

Optional. Installs system-level dependencies. Must be **idempotent** — safe to run on both first install and updates.

```bash
#!/bin/bash
set -e

apt-get update -qq
apt-get install -y --no-install-recommends ffmpeg curl
```

Runs inside the Docker container. If it fails, kiso warns the user and suggests asking the bot to fix it.

### `health_check` — skipping `deps.sh` on an already-installed wrapper

`kiso wrapper install X` is also the "ensure X is ready" command — users invoke it whenever a plan needs a wrapper, not just on first install. Without a health probe, every invocation re-runs `deps.sh` (which typically calls `apt-get`), paying a redundant cost and opening a window for `dpkg` lock contention with system package managers.

A wrapper can declare an optional `health_check` command in its `kiso.toml`:

```toml
[kiso]
name = "browser"
version = "0.3.0"
health_check = "chromium --version"
```

On `kiso wrapper install browser` when the wrapper is already installed, the CLI:

1. Runs `git pull --ff-only` to update source.
2. Reads `health_check` from the (possibly updated) `kiso.toml`.
3. Applies the gate:

| Condition                             | `deps.sh` runs? | Reason                     |
|---------------------------------------|-----------------|----------------------------|
| `--no-deps`                           | no              | explicit opt-out           |
| no `deps.sh` in repo                  | no              | nothing to run             |
| `--force`                             | yes             | explicit opt-in            |
| `git pull` advanced HEAD              | yes             | source updated             |
| no `health_check` declared            | yes             | legacy default (safe)      |
| `health_check` exits 0                | **no**          | system is healthy          |
| `health_check` exits non-zero         | yes             | probe failed, self-heal    |

**Zero regression for wrappers without `health_check`** — the legacy "always run `deps.sh`" path is the default. Opt-in per wrapper.

A good `health_check` is cheap (<1s), stable across the wrapper's supported platforms, and checks the *real* binary or library that `deps.sh` installs (e.g. `chromium --version`, `aider --version`, `python -c "import playwright"`). A probe that always passes even when the install is broken defeats the purpose; when in doubt, omit the field and the wrapper behaves as before.

**Manual recovery**: `kiso wrapper install X --force` bypasses the gate and runs `deps.sh` unconditionally.

## Installation

Only admins can install wrappers.

### Via CLI

```bash
# official (resolves from kiso-run org)
kiso wrapper install browser
# → clones git@github.com:kiso-run/wrapper-browser.git
# → ~/.kiso/instances/{instance}/wrappers/browser/

# unofficial (full git URL)
kiso wrapper install git@github.com:someone/my-wrapper.git
# → ~/.kiso/instances/{instance}/wrappers/github-com_someone_my-wrapper/

# unofficial with custom name
kiso wrapper install git@github.com:someone/my-wrapper.git --name custom
# → ~/.kiso/instances/{instance}/wrappers/custom/
```

### Unofficial Repo Warning

Unofficial repos trigger a confirmation prompt before install. If `deps.sh` is present, its contents are displayed for review before asking confirmation. Use `--no-deps` to skip `deps.sh`. Use `--show-deps` to display `deps.sh` without installing. See [security.md — Unofficial Package Warning](security.md#8-unofficial-package-warning) for the full warning text.

### Naming Convention

| Source | Name |
|---|---|
| Official (`kiso wrapper install search`) | `search` |
| Unofficial URL | `{domain}_{namespace}_{repo}` |
| Explicit `--name` | whatever you pass |

**URL to name algorithm:**
1. Strip the `.git` suffix
2. Normalize SSH URLs (`git@host:ns/repo` → `host/ns/repo`) and HTTPS URLs (`https://host/ns/repo` → `host/ns/repo`)
3. Lowercase everything
4. Replace `.` with `-` (in the domain)
5. Replace `/` with `_`

Examples:
```
git@github.com:sniku/jQuery-doubleScroll.git  → github-com_sniku_jquery-doublescroll
https://gitlab.com/team/cool-wrapper.git         → gitlab-com_team_cool-wrapper
```

### Install Flow

```
1. touch ~/.kiso/instances/{instance}/wrappers/{name}/.installing (prevents discovery during install)
2. git clone → ~/.kiso/instances/{instance}/wrappers/{name}/
3. Validate kiso.toml (exists? type=wrapper? has name? has [kiso.wrapper.args]?)
4. Validate run.py and pyproject.toml exist — fail if missing
5. If unofficial repo → warn user, ask confirmation (see security.md)
6. uv sync (pyproject.toml → .venv)  ← must run before deps.sh
7. If deps.sh exists → run it (skipped with --no-deps)
   ⚠ on failure: warn user, suggest "ask the bot to fix deps for wrapper {name}"
   Note: deps.sh may call binaries installed by uv (e.g. playwright install webkit)
8. Check [kiso.deps].bin (verify with `which`)
9. Check [kiso.wrapper.env] vars
   ⚠ KISO_TOOL_SEARCH_API_KEY not set (warn, don't block)
10. rm ~/.kiso/instances/{instance}/wrappers/{name}/.installing
```

### Update / Remove

```bash
kiso wrapper update search          # git pull + deps.sh + uv sync
kiso wrapper update all             # update all installed wrappers
kiso wrapper remove search
kiso wrapper list                   # list installed wrappers
```

```
$ kiso wrapper list
  search   0.1.0  — Web search using Brave Search API
  aider    0.3.2  — Code editing wrapper using LLM
  browser  0.1.0  — Headless WebKit browser automation
```

### Search

```bash
kiso wrapper search [query]
# → fetches https://raw.githubusercontent.com/kiso-run/core/main/registry.json
# → matches by name first, then by description
```

## Execution

When the worker encounters a `wrapper` task:

1. Parses `args` from JSON string, validates against the schema in `kiso.toml`
2. Builds input JSON (parsed args as object + session + workspace path + scoped ephemeral secrets as dict + plan outputs from preceding tasks)
3. Pipes input JSON to stdin: `.venv/bin/python KISO_DIR/wrappers/search/run.py` with `cwd=KISO_DIR/sessions/{session}`
4. Captures stdout (output) and stderr (debug)
5. Sanitizes output (strips known secret values — plaintext, base64, URL-encoded)
6. Stores task result in DB (status, output)
7. Passes to the reviewer (all exec/wrapper tasks are always reviewed)

## Validation

Before execution, wrapper task args are validated at plan time:

- **Schema validation**: args checked against `kiso.toml` arg schema (required fields, types)
- **Semantic validation**: wrapper-specific checks (e.g., aider: instruction in `message`, not in `files`)
- **Browser URL scheme**: browser wrapper rejects `file://` URLs — browser is for web content (http/https). Use exec with `cat`/`head` to read local files.

## Discovery

Scanned from `KISO_DIR/wrappers/` before each planner call. Reads `kiso.toml` from each directory (skips directories with `.installing` marker file). The planner sees one-liners and args schemas (see [What the Planner Sees](#what-the-planner-sees) for format) and decides whether to use a wrapper or a plain `exec` task.

The wrapper list is scanned on every planner call — no caching. Newly installed or removed wrappers are immediately visible to the server without a restart. The scan is fast (TOML parse of a handful of files, microseconds) and negligible compared to the LLM call that follows.

## Why Subprocesses

- **Isolation**: own venv, no dependency conflicts.
- **Simplicity**: JSON in, text out. No dynamic imports or async coordination.
- **Safety**: crashing wrapper doesn't take down the worker.
- **Language-agnostic**: run.py can call anything (Node, Go, curl, etc.).
