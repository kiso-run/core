# Skills

A skill is a git-cloned package in `~/.kiso/skills/{name}/` with a standard structure. Skills run as subprocesses, each in its own isolated environment managed by `uv`.

## Structure

```
~/.kiso/skills/
├── search/
│   ├── kiso.toml           # manifest (required)
│   ├── pyproject.toml      # python dependencies (uv-managed)
│   ├── run.py              # entry point (required)
│   ├── SKILL.md            # docs for the planner (required)
│   ├── deps.sh             # system deps installer (optional, idempotent)
│   ├── README.md           # human docs
│   └── .venv/              # created by uv on install
└── .../
```

A directory is a valid skill if it contains `kiso.toml` (with `type = "skill"`), `run.py`, and `SKILL.md`.

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

## SKILL.md

Docs for the planner and worker. The planner sees only the one-liner from `kiso.toml`. The worker receives the full `SKILL.md` to understand how to use the skill.

Must start with a one-line summary after the heading.

```markdown
# search — Web search using Brave Search API

## When to use
- The user asks to look up information on the web
- Current data or facts are needed

## Arguments
- query (required): search query string
- max_results (optional): number of results, default 5
```

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

Runs inside the Docker container, so `apt install` works without sudo. If it fails, kiso warns the user and suggests asking the bot to fix it.

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

### Naming Convention

| Source | Name |
|---|---|
| Official (`kiso skill install search`) | `search` |
| Unofficial URL | `{domain}_{namespace}_{repo}` |
| Explicit `--name` | whatever you pass |

URL to name: lowercase, `.` → `-`, `/` → `_`, strip `skill-` prefix if present.

Examples:
```
git@github.com:sniku/jQuery-doubleScroll.git  → github-com_sniku_jquery-doublescroll
https://gitlab.com/team/cool-skill.git        → gitlab-com_team_cool-skill
```

### Install Flow

```
1. git clone → ~/.kiso/skills/{name}/
2. Validate kiso.toml (exists? type=skill? has name?)
3. Validate run.py and SKILL.md exist
4. If deps.sh exists → run it
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

1. Builds input JSON (args + session + workspace path + secrets)
2. Picks Python: `.venv/bin/python` (created by `uv`)
3. Runs: `{python} ~/.kiso/skills/search/run.py < input.json` with `cwd=~/.kiso/sessions/{session}`
4. Captures stdout (output) and stderr (debug)
5. Sanitizes output (strips known secret values)
6. If `review: true`, passes to the reviewer

## Discovery

Rescanned from `~/.kiso/skills/` before each planner call. No restart needed.

The planner sees one-liners from `kiso.toml`:

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
