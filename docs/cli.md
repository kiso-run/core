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
   - POSTs to `/msg` on localhost (or `--api`) with `user` = Linux username
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

The API token is read from `~/.kiso/config.toml`: the CLI always uses the token named `cli` from the `[tokens]` section.

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

See [skills.md — Install Flow](skills.md#install-flow) for the full 10-step sequence (includes `.installing` marker to prevent discovery during install).

### Naming

| Source | Installed as |
|---|---|
| `kiso skill install search` | `~/.kiso/skills/search/` |
| `kiso skill install git@github.com:foo/bar.git` | `~/.kiso/skills/github-com_foo_bar/` |
| `kiso skill install <url> --name custom` | `~/.kiso/skills/custom/` |

URL to name: see [skills.md — Naming Convention](skills.md#naming-convention) for the full algorithm.

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

## Session Management

```bash
kiso sessions                                  # list sessions you participate in
kiso sessions --all                            # list all sessions (admin only)
```

Output:

```
$ kiso sessions
  laptop@marco    — last activity: 2m ago
  dev-backend     — last activity: 1h ago

$ kiso sessions --all
  laptop@marco    — last activity: 2m ago
  dev-backend     — last activity: 1h ago
  discord_dev     — connector: discord, last activity: 5m ago
  discord_general — connector: discord, last activity: 30m ago
```

Non-admins see only sessions they have participated in. Admins with `--all` see every session including connector-managed ones. See [api.md — GET /sessions](api.md#get-sessions).

## Deploy Secret Management

Only admins can manage deploy secrets.

```bash
kiso env set KISO_SKILL_SEARCH_API_KEY sk-...  # set a deploy secret
kiso env get KISO_SKILL_SEARCH_API_KEY         # show value
kiso env list                                  # list all deploy secrets (names only)
kiso env delete KISO_SKILL_SEARCH_API_KEY      # remove a deploy secret
kiso env reload                                # hot-reload .env without restart
```

Secrets are stored in `~/.kiso/.env` and loaded into the process environment. `kiso env reload` calls `POST /admin/reload-env` to hot-reload without restarting the server. See [security.md — Deploy Secrets](security.md#deploy-secrets).

## Notes

- `kiso serve` starts the HTTP server (used in Docker CMD, not typically run directly).
- Chat mode is a thin HTTP wrapper — all intelligence lives in the server.
- Works against a remote server (`--api`) — useful for running kiso on a VPS.
