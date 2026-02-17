# Skills

A skill is a git-cloned package in `~/.kiso/skills/{name}/`. Runs as a subprocess in a `uv`-managed venv.

## Structure

```
~/.kiso/skills/
├── search/
│   ├── kiso.toml           # manifest (required) — identity, args schema, deps
│   ├── pyproject.toml      # python dependencies (required, uv-managed)
│   ├── run.py              # entry point (required)
│   ├── deps.sh             # system deps installer (optional, idempotent)
│   ├── README.md           # docs for humans (optional)
│   └── .venv/              # created by uv on install
└── .../
```

A directory is a valid skill if it contains `kiso.toml` (with `type = "skill"`), `pyproject.toml`, and `run.py`.

All three are required — install fails if any is missing.

## kiso.toml

The single source of truth. Declares what this skill is, what arguments it takes, what it needs, and what system deps it requires.

```toml
[kiso]
type = "skill"
name = "search"
version = "0.1.0"
description = "Web search using Brave Search API"

[kiso.skill]
summary = "Web search using Brave Search API"    # one-liner for the planner
# session_secrets = ["github_token"]             # user-provided credentials (not needed for this skill)

[kiso.skill.args]
query = { type = "string", required = true, description = "search query" }
max_results = { type = "int", required = false, default = 5, description = "number of results to return" }

[kiso.skill.env]
api_key = { required = true }     # → KISO_SKILL_SEARCH_API_KEY

[kiso.deps]
python = ">=3.11"
bin = ["curl"]                    # checked with `which` after install
```

### Two Kinds of Secrets

- `[kiso.skill.env]` → **deploy secrets** (env vars, set by admin via `kiso env`, passed via subprocess environment)
- `session_secrets` → **ephemeral secrets** (user-provided at runtime, in worker memory only — never persisted. Passed via input JSON — only declared ones)

See [security.md — Secrets](security.md#5-secrets) for the full comparison and scoping rules.

### What the Planner Sees

The planner receives the one-liner and the args schema:

```
Available skills:
- search — Web search using Brave Search API
  args: query (string, required): search query
        max_results (int, optional, default=5): number of results to return
```

This is enough for the planner to generate correct invocations. No ambiguity.

### Env Var Naming

Env vars follow the convention `KISO_SKILL_{NAME}_{KEY}`, built automatically:

| Manifest key | Env var |
|---|---|
| `api_key` | `KISO_SKILL_SEARCH_API_KEY` |
| `token` | `KISO_SKILL_SEARCH_TOKEN` |

Name and key are uppercased, `-` becomes `_`.

### Version

Display metadata. Shown in `kiso skill list` output. Kiso does not enforce version compatibility — updates are always `git pull` to latest.

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
    # skill logic here
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
- `plan_outputs`: outputs from preceding tasks in the same plan. See [flow.md — Task Output Chaining](flow.md#task-output-chaining). Empty array if this is the first task. Skills can use it or ignore it.

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

## Installation

Only admins can install skills.

### Via CLI

```bash
# official (resolves from kiso-run org)
kiso skill install search
# → clones git@github.com:kiso-run/skill-search.git
# → ~/.kiso/skills/search/

# unofficial (full git URL)
kiso skill install git@github.com:someone/my-skill.git
# → ~/.kiso/skills/github-com_someone_my-skill/

# unofficial with custom name
kiso skill install git@github.com:someone/my-skill.git --name custom
# → ~/.kiso/skills/custom/
```

### Unofficial Repo Warning

Unofficial repos trigger a confirmation prompt before install. If `deps.sh` is present, its contents are displayed for review before asking confirmation. Use `--no-deps` to skip `deps.sh`. Use `--show-deps` to display `deps.sh` without installing. See [security.md — Unofficial Package Warning](security.md#8-unofficial-package-warning) for the full warning text.

### Naming Convention

| Source | Name |
|---|---|
| Official (`kiso skill install search`) | `search` |
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
https://gitlab.com/team/cool-skill.git        → gitlab-com_team_cool-skill
```

### Install Flow

```
1. touch ~/.kiso/skills/{name}/.installing (prevents discovery during install)
2. git clone → ~/.kiso/skills/{name}/
3. Validate kiso.toml (exists? type=skill? has name? has [kiso.skill.args]?)
4. Validate run.py and pyproject.toml exist — fail if missing
5. If unofficial repo → warn user, ask confirmation (see security.md)
6. If deps.sh exists → run it (skipped with --no-deps)
   ⚠ on failure: warn user, suggest "ask the bot to fix deps for skill {name}"
7. uv sync (pyproject.toml → .venv)
8. Check [kiso.deps].bin (verify with `which`)
9. Check [kiso.skill.env] vars
   ⚠ KISO_SKILL_SEARCH_API_KEY not set (warn, don't block)
10. rm ~/.kiso/skills/{name}/.installing
```

### Update / Remove

```bash
kiso skill update search          # git pull + deps.sh + uv sync
kiso skill update all             # update all installed skills
kiso skill remove search
kiso skill list                   # list installed skills
```

```
$ kiso skill list
  search  0.1.0  — Web search using Brave Search API
  aider   0.3.2  — Code editing tool using LLM
```

### Search

```bash
kiso skill search [query]
# → GET https://api.github.com/search/repositories?q=org:kiso-run+topic:kiso-skill
```

## Execution

When the worker encounters a `skill` task:

1. Parses `args` from JSON string, validates against the schema in `kiso.toml`
2. Builds input JSON (parsed args as object + session + workspace path + scoped ephemeral secrets as dict + plan outputs from preceding tasks)
3. Pipes input JSON to stdin: `.venv/bin/python ~/.kiso/skills/search/run.py` with `cwd=~/.kiso/sessions/{session}`
4. Captures stdout (output) and stderr (debug)
5. Sanitizes output (strips known secret values — plaintext, base64, URL-encoded)
6. Stores task result in DB (status, output)
7. Passes to the reviewer (all exec/skill tasks are always reviewed)

## Discovery

Rescanned from `~/.kiso/skills/` before each planner call. Reads `kiso.toml` from each directory (skips directories with `.installing` marker file). No restart needed. The planner sees one-liners and args schemas (see [What the Planner Sees](#what-the-planner-sees) for format) and decides whether to use a skill or a plain `exec` task.

## Why Subprocesses

- **Isolation**: own venv, no dependency conflicts.
- **Simplicity**: JSON in, text out. No dynamic imports or async coordination.
- **Safety**: crashing skill doesn't take down the worker.
- **Language-agnostic**: run.py can call anything (Node, Go, curl, etc.).
