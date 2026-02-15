# Skills

A skill is a git-cloned package in `~/.kiso/skills/{name}/` with a standard structure. Skills run as subprocesses, each in its own isolated environment managed by `uv`.

## Structure

```
~/.kiso/skills/
├── search/
│   ├── kiso.toml           # manifest (required)
│   ├── pyproject.toml      # python dependencies (required, uv-managed)
│   ├── run.py              # entry point (required)
│   ├── SKILL.md            # docs for the worker (required)
│   ├── deps.sh             # system deps installer (optional, idempotent)
│   ├── README.md           # human docs
│   └── .venv/              # created by uv on install
└── .../
```

A directory is a valid skill if it contains `kiso.toml` (with `type = "skill"`), `pyproject.toml`, `run.py`, and `SKILL.md`.

All four are required. No fallbacks — if `pyproject.toml` is missing, install fails.

## kiso.toml

The manifest. Declares what this package is.

```toml
[kiso]
type = "skill"
name = "search"
version = "0.1.0"
description = "Web search using Brave Search API"

[kiso.skill]
summary = "Web search using Brave Search API"    # one-liner for the planner

[kiso.skill.env]
api_key = { required = true }     # → KISO_SKILL_SEARCH_API_KEY

[kiso.skill.secrets]
secrets = ["api_key"]             # which session secrets this skill receives

[kiso.deps]
python = ">=3.11"
bin = ["curl"]                    # checked with `which` after install
```

### Env Var Naming

Env vars follow the convention `KISO_SKILL_{NAME}_{KEY}`, built automatically:

| Manifest key | Env var |
|---|---|
| `api_key` | `KISO_SKILL_SEARCH_API_KEY` |
| `token` | `KISO_SKILL_SEARCH_TOKEN` |

Name and key are uppercased, `-` becomes `_`.

### Secret Scoping

Skills declare which session secrets they need in `[kiso.skill.secrets]`. Kiso passes **only those secrets** to the skill at runtime — not the entire session secrets bag. If the field is missing, the skill receives no secrets.

This limits blast radius: a compromised skill can only leak the secrets it declared.

## SKILL.md

Documentation for the worker. The worker receives the full content of this file to understand how to use the skill — what arguments to pass, when it's appropriate, what to expect.

Free format. Write whatever helps the worker use the skill correctly.

```markdown
# search

Web search using Brave Search API.

## When to use
- The user asks to look up information on the web
- Current data or facts are needed

## Arguments
- query (required): search query string
- max_results (optional): number of results, default 5

## Output
Returns a text summary of the top search results with titles, URLs, and snippets.

## Notes
- Requires KISO_SKILL_SEARCH_API_KEY to be set
- Rate limited to 1 request per second
```

The planner does **not** see SKILL.md. It sees only the one-liner from `kiso.toml` (`[kiso.skill] summary`).

## run.py

Standalone script. Reads JSON from stdin, writes result to stdout.

```python
#!/usr/bin/env python
import json
import sys


def run(args, context):
    """
    args:    arguments passed by the planner (dict)
    context: {"session", "workspace", "secrets"} (dict)
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
  "args": {"query": "python async patterns"},
  "session": "dev-backend",
  "workspace": "/home/user/.kiso/sessions/dev-backend",
  "secrets": {"api_key": "sk-abc123"}
}
```

`secrets` contains **only** the keys declared in `[kiso.skill.secrets]`, not the full session secrets.

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

URL to name: lowercase, `.` → `-`, `/` → `_`.

Examples:
```
git@github.com:sniku/jQuery-doubleScroll.git  → github-com_sniku_jquery-doublescroll
https://gitlab.com/team/cool-skill.git        → gitlab-com_team_cool-skill
```

### Install Flow

```
1. git clone → ~/.kiso/skills/{name}/
2. Validate kiso.toml (exists? type=skill? has name?)
3. Validate run.py, pyproject.toml, and SKILL.md exist — fail if any missing
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

### Search

```bash
kiso skill search [query]
# → GET https://api.github.com/search/repositories?q=org:kiso-run+topic:kiso-skill
```

## Execution

When the worker encounters `{"type": "skill", "skill": "search", "args": {...}}`:

1. Builds input JSON (args + session + workspace path + scoped secrets)
2. Runs: `.venv/bin/python ~/.kiso/skills/search/run.py < input.json` with `cwd=~/.kiso/sessions/{session}`
3. Captures stdout (output) and stderr (debug)
4. Sanitizes output (strips known secret values)
5. Stores task result in DB (status, output)
6. If `review: true`, passes to the reviewer

## Discovery

Rescanned from `~/.kiso/skills/` before each planner call. Reads `kiso.toml` from each skill directory. No restart needed.

The planner sees one-liners from `[kiso.skill] summary`:

```
Available skills:
- search — Web search using Brave Search API
- aider — Code editing tool using LLM to apply changes in natural language
```

The planner decides whether to use a skill or a plain `exec` task.

## Why Subprocesses

- **Isolation**: each skill has its own venv. No dependency conflicts.
- **Simplicity**: no dynamic imports, no async coordination. JSON in, text out.
- **Safety**: a crashing skill doesn't take down the worker.
- **Language-agnostic**: run.py can internally call anything (Node, Go, curl, etc.).
