# CLI

The `kiso` command serves two purposes: interactive chat client and management tool.

## Chat Mode

```bash
kiso                                           # default session: hostname@user
kiso --session my-project                      # specific session
kiso --api http://remote:8333                  # remote server
```

### Default Session

If not specified, the session is: `{hostname}@{username}`

The username comes from the Linux user (`whoami`). Example: `laptop@marco`

### Behavior

1. Generates session ID (default or from `--session`)
2. Shows interactive prompt
3. On each user input:
   - POSTs to `/msg` on localhost (or `--api`) with `user` = Linux username, empty webhook
   - Polls `GET /status/{session}?after={last_task_id}`
   - Prints results as they arrive
4. Loops until `Ctrl+C` or `exit`

### Output

```
$ kiso --session dev
You: create a base FastAPI project
[planning] 3 tasks queued
[1/3] exec: creating project structure ... done
[2/3] skill:aider: writing base code ... done
[3/3] msg: final response
Bot: Created the project in sessions/dev/...

You: add authentication
[planning] 2 tasks queued
...
```

The API token is read from `~/.kiso/config.json`.

## Skill Management

Only admins can install, update, and remove skills.

```bash
kiso skill search [query]                      # search official skills on GitHub
kiso skill install <name>                      # official: resolves from kiso-run org
kiso skill install <git-url>                   # unofficial: clone from any git URL
kiso skill install <git-url> --name foo        # unofficial with custom name
kiso skill install <git-url> --no-deps         # skip deps.sh execution
kiso skill update <name>                       # git pull + deps.sh + uv sync
kiso skill update all                          # update all installed skills
kiso skill remove <name>
kiso skill list                                # list installed skills
```

### Search

Queries the GitHub API for official skills:

```bash
$ kiso skill search
  search      — Web search using Brave Search API
  aider       — Code editing tool using LLM
  screenshot  — Take screenshots of web pages

$ kiso skill search web
  search      — Web search using Brave Search API
  screenshot  — Take screenshots of web pages
```

### Install Flow

```
1. git clone → ~/.kiso/skills/{name}/
2. Validate kiso.toml (exists? type=skill?)
3. Validate run.py, pyproject.toml, and SKILL.md exist — fail if any missing
4. If unofficial repo → warn user, ask confirmation
5. If deps.sh exists → run it (skipped with --no-deps)
   ⚠ on failure: warn user, suggest "ask the bot to fix deps for skill {name}"
6. uv sync (pyproject.toml → .venv)
7. Check [kiso.deps].bin
8. Check env vars from [kiso.skill.env]
```

### Naming

| Source | Installed as |
|---|---|
| `kiso skill install search` | `~/.kiso/skills/search/` |
| `kiso skill install git@github.com:foo/bar.git` | `~/.kiso/skills/github-com_foo_bar/` |
| `kiso skill install <url> --name custom` | `~/.kiso/skills/custom/` |

URL to name: lowercase, `.` → `-`, `/` → `_`.

## Connector Management

Only admins can install, update, and remove connectors.

```bash
kiso connector search [query]                  # search official connectors on GitHub
kiso connector install <name>                  # official: resolves from kiso-run org
kiso connector install <git-url>               # unofficial: clone from any git URL
kiso connector install <git-url> --name foo    # unofficial with custom name
kiso connector install <git-url> --no-deps     # skip deps.sh execution
kiso connector update <name>
kiso connector update all
kiso connector remove <name>
kiso connector list
```

### Run / Stop / Status

Kiso manages connectors as daemon processes:

```bash
kiso connector discord run                     # start as daemon
kiso connector discord stop                    # stop the daemon
kiso connector discord status                  # check if running
```

## Notes

- Chat mode has no agent logic. It's just an HTTP wrapper.
- All intelligence lives in the server.
- Works against a remote server — useful for running kiso on a VPS and working from a laptop.
- Session logs: `tail -f ~/.kiso/sessions/{session}/session.log`
- Only admins can install/update/remove skills and connectors.
- Unofficial packages show a warning before installation.
