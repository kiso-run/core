# Skill Authoring Reference

## File Structure

```
~/.kiso/skills/{name}/
├── kiso.toml           # manifest (required)
├── pyproject.toml      # python deps (required, uv-managed)
├── run.py              # entry point (required)
├── config.example.toml # default config (optional, copied to config.toml on install)
├── deps.sh             # system deps (optional, must be idempotent)
├── .gitignore
├── tests/              # tests (recommended)
│   └── test_run.py
└── .venv/              # created by uv on install
```

No `src/` layout. Kiso runs `.venv/bin/python run.py` as a subprocess. All code lives in `run.py` or modules imported by it.

## kiso.toml

```toml
[kiso]
type = "skill"
name = "search"
version = "0.1.0"
description = "Web search using Brave Search API"

[kiso.skill]
summary = "Web search using Brave Search API"    # one-liner shown to planner
# session_secrets = ["github_token"]             # user-provided credentials (optional)
# usage_guide = "Use short queries."             # operational guidance for planner

[kiso.skill.args]
query = { type = "string", required = true, description = "search query" }
max_results = { type = "int", required = false, default = 5, description = "max results" }

[kiso.skill.env]
api_key = { required = true }   # → env var KISO_SKILL_SEARCH_API_KEY

[kiso.deps]
python = ">=3.11"
bin = ["curl"]                  # checked with `which` after install
```

### Secrets

- `[kiso.skill.env]` → deploy secrets (env vars, set via `kiso env`)
- `session_secrets` → ephemeral (user-provided at runtime, in memory only, passed via input JSON)

### Env Var Naming

Convention: `KISO_SKILL_{NAME}_{KEY}` (uppercased, `-` → `_`).

## config.example.toml

Optional. For non-secret, deployment-specific config. Shipped in repo, copied to `config.toml` on install. `config.toml` is gitignored.

```toml
# Example: search backend selection
backend = "brave"
```

Load from `run.py`:

```python
import tomllib
from pathlib import Path

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.toml"
    if not config_path.exists():
        return {}  # sensible defaults
    with open(config_path, "rb") as f:
        return tomllib.load(f)
```

No secrets in this file — secrets come from env vars declared in `kiso.toml`.

## run.py — stdin/stdout contract

```python
import json, sys

def run(args, context):
    # args: dict from planner
    # context: full input (args, session, workspace, session_secrets, plan_outputs)
    return "result text"

if __name__ == "__main__":
    data = json.load(sys.stdin)
    result = run(data["args"], data)
    print(result)
```

### Input (stdin JSON)

```json
{
  "args": {"query": "python async"},
  "session": "dev-backend",
  "workspace": "/root/.kiso/sessions/dev-backend",
  "session_secrets": {"github_token": "ghp_abc123"},
  "plan_outputs": [
    {"index": 1, "type": "exec", "detail": "ls", "output": "main.py", "status": "done"}
  ]
}
```

- `session_secrets`: only keys declared in `kiso.toml`, not all session credentials
- `plan_outputs`: outputs from preceding tasks in same plan (empty if first task)

### Output (stdout) and error handling

- **Success**: print result text to stdout, exit 0
- **Error**: print debug details to **stderr**, print user-friendly message to stdout (e.g. `Search failed: API key not configured.`), exit 1
- Kiso marks exit 1 as task failure. The reviewer sees the output and can trigger a replan.
- **Never print secrets** to stdout or stderr — kiso sanitizes known values, but don't rely on it

### SIGTERM handling

For long-running skills, forward SIGTERM to child processes for graceful shutdown:

```python
import signal
def handle_sigterm(signum, frame):
    # clean up, terminate child processes
    sys.exit(0)
signal.signal(signal.SIGTERM, handle_sigterm)
```

## deps.sh

Must be idempotent (safe on both install and update). Runs inside container.

```bash
#!/bin/bash
set -e
apt-get update -qq
apt-get install -y --no-install-recommends ffmpeg curl
```

## .gitignore

```
__pycache__/
*.pyc
.venv/
config.toml
.installing
```

## Testing

Skills have their own venv and `pyproject.toml`, so tests live inside the skill repo. Assumes kiso is installed and the skill's venv is set up (`kiso skill install` or `uv sync`).

### pyproject.toml — add test deps

```toml
[dependency-groups]
dev = ["pytest>=8"]
```

### Unit test — test `run()` directly

```python
# tests/test_run.py
from run import run

def test_basic_search():
    args = {"query": "python async", "max_results": 3}
    context = {
        "args": args,
        "session": "test",
        "workspace": "/tmp/test-workspace",
        "session_secrets": {},
        "plan_outputs": [],
    }
    result = run(args, context)
    assert isinstance(result, str)
    assert len(result) > 0
```

### Integration test — test stdin/stdout contract

```python
# tests/test_integration.py
import json
import subprocess

def test_stdin_stdout_contract():
    input_data = {
        "args": {"query": "test"},
        "session": "test",
        "workspace": "/tmp/test-workspace",
        "session_secrets": {},
        "plan_outputs": [],
    }
    result = subprocess.run(
        [".venv/bin/python", "run.py"],
        input=json.dumps(input_data),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert len(result.stdout.strip()) > 0
```

### Running tests

```bash
cd ~/.kiso/skills/{name}
uv run --group dev pytest tests/ -v
```

### Tips

- Test `run()` directly for fast unit tests (no subprocess overhead)
- Use the subprocess test to verify the full stdin/stdout contract
- Mock external APIs (HTTP calls, etc.) for offline tests; add a separate marker for live tests that hit real services
- Test edge cases: missing optional args, empty `plan_outputs`, large inputs

## License

Official skills use the **MIT License**. Third-party skills can use any license.

## Key Conventions

- Install: `kiso skill install {name|url}` (official: `kiso-run/skill-{name}`)
- Discovery: rescanned from `~/.kiso/skills/` before each planner call
- Execution: `.venv/bin/python run.py` with JSON piped to stdin, `cwd=session workspace`
- Environment: only `PATH` + declared env vars from `[kiso.skill.env]`
- Exit 0 = success, exit 1 = failure (kiso marks as failed task)
- Output sanitized (secrets stripped) before storage and LLM inclusion
- All skill tasks are reviewed by the reviewer after execution
- `uv` for dependency management — kiso runs `uv sync` on install
