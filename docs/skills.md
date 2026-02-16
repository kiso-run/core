# Skills

A skill is a git-cloned package in `~/.kiso/skills/{name}/` with a standard structure. Skills run as subprocesses, each in its own isolated environment managed by `uv`.

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

All three are required. No fallbacks — if any is missing, install fails.

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

Skills can receive two kinds of credentials — **deploy secrets** (env vars, set once by admin) and **session secrets** (user-provided at runtime). See [security.md](security.md#4-secrets) for the full comparison.

In `kiso.toml`:
- `[kiso.skill.env]` declares deploy secrets → passed via subprocess environment
- `session_secrets` declares which session secrets the skill needs → passed via input JSON

`session_secrets` lists which user-provided credentials this skill receives at runtime. Kiso passes **only those** — not the entire session bag. If omitted, the skill receives no session secrets. This limits blast radius.

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

Standalone script. Reads JSON from stdin, writes result to stdout.

```python
import json
import sys


def run(args, context):
    """
    args:    arguments passed by the planner (dict)
    context: {"session", "workspace", "session_secrets"} (dict)
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
  "session_secrets": {"github_token": "ghp_abc123"}
}
```

`session_secrets` contains **only** the keys declared in `kiso.toml`, not the full session credentials.

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

When installing from a non-official source (not `kiso-run` org), kiso warns:

```
⚠ This is an unofficial package from github.com:someone/my-skill.
  deps.sh will be executed and may install system packages.
  Review the repo before proceeding.
  Continue? [y/N]
```

Use `--no-deps` to skip `deps.sh` execution:

```bash
kiso skill install git@github.com:someone/my-skill.git --no-deps
```

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
1. git clone → ~/.kiso/skills/{name}/
2. Validate kiso.toml (exists? type=skill? has name? has [kiso.skill.args]?)
3. Validate run.py and pyproject.toml exist — fail if any missing
4. If deps.sh exists → run it (with warning/confirmation for unofficial repos)
   ⚠ on failure: warn user, suggest "ask the bot to fix deps for skill {name}"
5. uv sync (pyproject.toml → .venv)
6. Check [kiso.deps].bin (verify with `which`)
7. Check [kiso.skill.env] vars
   ⚠ KISO_SKILL_SEARCH_API_KEY not set (warn, don't block)
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

When the worker encounters `{"type": "skill", "skill": "search", "args": {...}}`:

1. Validates args against the schema in `kiso.toml`
2. Builds input JSON (args + session + workspace path + scoped session secrets)
3. Pipes input JSON to stdin: `.venv/bin/python ~/.kiso/skills/search/run.py` with `cwd=~/.kiso/sessions/{session}`
4. Captures stdout (output) and stderr (debug)
5. Sanitizes output (strips known secret values)
6. Stores task result in DB (status, output)
7. If `review: true`, passes to the reviewer

## Discovery

Rescanned from `~/.kiso/skills/` before each planner call. Reads `kiso.toml` from each skill directory. No restart needed.

The planner sees one-liners and args schemas:

```
Available skills:
- search — Web search using Brave Search API
  args: query (string, required): search query
        max_results (int, optional, default=5): number of results to return
- aider — Code editing tool using LLM to apply changes in natural language
  args: message (string, required): description of the change
        files (list, optional): files to operate on
```

The planner decides whether to use a skill or a plain `exec` task.

## Why Subprocesses

- **Isolation**: each skill has its own venv. No dependency conflicts.
- **Simplicity**: no dynamic imports, no async coordination. JSON in, text out.
- **Safety**: a crashing skill doesn't take down the worker.
- **Language-agnostic**: run.py can internally call anything (Node, Go, curl, etc.).
