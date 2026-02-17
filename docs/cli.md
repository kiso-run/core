# CLI

The `kiso` command serves two purposes: interactive chat client and management tool.

## Chat Mode

```bash
kiso                                           # default session: hostname@user
kiso --session my-project                      # specific session
kiso --api http://remote:8333                  # remote server
kiso --quiet                                   # only show bot messages
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--session SESSION` | `{hostname}@{username}` | Session identifier |
| `--api URL` | `http://localhost:8333` | Server URL |
| `--quiet` / `-q` | off | Only show `msg` task content (hide decision flow) |

By default, the CLI shows the **full decision flow** — planning, task execution, review verdicts, replans, and bot messages. This is the primary way to understand what kiso is doing and why. Use `--quiet` to suppress everything except the final bot responses.

The API token is read from `~/.kiso/config.toml`: the CLI always uses the token named `cli` from the `[tokens]` section.

### Default Session

If not specified, the session is: `{hostname}@{username}`

The username comes from the Linux user (`whoami`). Example: `laptop@marco`

### Behavior

1. Generates session ID (default or from `--session`)
2. Shows interactive prompt
3. On each user input:
   - POSTs to `/msg` on localhost (or `--api`) with `user` = Linux username
   - Polls `GET /status/{session}?after={last_task_id}` until plan completes
   - Renders each event as it arrives (see [Display Rendering](#display-rendering))
4. Loops until `Ctrl+C` or `exit`

### Display Rendering

The CLI renders the full execution flow in real time. Every decision the system makes is visible to the user. The renderer reads task updates from `/status` polling and maps each event to a display element.

#### Display Modes

| Mode | Shows | When |
|------|-------|------|
| **Default** (verbose) | Everything: plan goal, task progress, output, review verdicts, replans, learnings, bot messages | Always, unless `--quiet` |
| **Quiet** (`--quiet`) | Only `msg` task content — the bot's actual responses | When you don't care about internals |

#### Visual Elements

**Colors** (256-color terminal, graceful fallback to no-color if unsupported):

| Element | Color | Purpose |
|---------|-------|---------|
| Plan goal | **bold cyan** | Distinguishes the high-level objective |
| Task header (`exec`, `skill`) | **yellow** | Work in progress |
| Task header (`msg`) | **green** | Bot response |
| Task output | **dim** | De-emphasized, secondary information |
| Review: ok | **green** | Success |
| Review: replan | **bold red** | Failure, attention needed |
| Replan reason | **red** | Why the plan changed |
| Learning | **magenta** | Knowledge extracted |
| Error/cancel | **bold red** | Something went wrong |
| Spinner | **cyan** | Activity indicator |

**Icons** (Unicode, with ASCII fallback for non-Unicode terminals):

| Event | Unicode | ASCII | Meaning |
|-------|---------|-------|---------|
| Plan | `◆` | `*` | New plan started |
| exec task | `▶` | `>` | Shell command running |
| skill task | `⚡` | `!` | Skill running |
| msg task | `💬` | `"` | Bot message |
| Review ok | `✓` | `ok` | Task passed review |
| Review replan | `✗` | `FAIL` | Task failed review |
| Replan | `↻` | `~>` | Replanning |
| Learning | `📝` | `+` | Knowledge extracted |
| Cancel | `⊘` | `X` | Plan cancelled |

#### Full Flow Example (Default Mode)

```
$ kiso --session dev
You: deploy the app to fly.io

◆ Plan: Deploy application to fly.io (3 tasks)

▶ [1/3] exec: fly launch --no-deploy --name dev-app
  ┊ Scanning source code... Detected a FastAPI app
  ┊ Creating app "dev-app" in organization "personal"
  ┊ Wrote config file fly.toml
  ✓ review: ok

⚡ [2/3] skill:aider: Update fly.toml to add health check endpoint
  ┊ Edited fly.toml: added [[services.http_checks]] section
  ✓ review: ok

▶ [3/3] exec: fly deploy
  ┊ ==> Building image with Docker
  ┊ ...
  ┊ Error: failed to fetch an image or build from source: no such file: Dockerfile
  ✗ review: replan — "No Dockerfile found. Need to create one first."

↻ Replan: Create Dockerfile then deploy (3 tasks)

▶ [1/3] exec: cat > Dockerfile << 'EOF' ...
  ✓ review: ok

▶ [2/3] exec: fly deploy
  ┊ ==> Building image
  ┊ ==> Pushing image
  ┊ ==> Monitoring deployment
  ┊ v0 deployed successfully
  ✓ review: ok

💬 [3/3] msg
Bot: Deployed to fly.io. The app is live at https://dev-app.fly.dev.
     I had to create a Dockerfile first since one wasn't present.
     The health check is configured at /health.

You: _
```

#### Quiet Mode Example

```
$ kiso --session dev --quiet
You: deploy the app to fly.io
Bot: Deployed to fly.io. The app is live at https://dev-app.fly.dev.
     I had to create a Dockerfile first since one wasn't present.
     The health check is configured at /health.

You: _
```

#### Replan Display

When a review triggers a replan, the CLI shows:

1. The `✗` verdict with the reviewer's reason (red)
2. A `↻ Replan:` line with the new goal (bold)
3. The new task list continues with fresh numbering

If max replan depth is reached, the CLI shows an error:

```
✗ review: replan — "Still can't connect to the database"
⊘ Max replans reached (3). Giving up.

💬 msg
Bot: I wasn't able to complete the task. After 3 attempts, the database
     connection keeps failing. Please check that PostgreSQL is running
     and the connection string is correct.
```

#### Output Truncation

Long task output (exec stdout/stderr, skill output) is truncated in the terminal to keep the display readable:

- **Default**: show first 20 lines, collapse the rest behind `... (N more lines, press Enter to expand)`
- If the terminal has fewer than 40 rows, reduce to 10 lines
- `msg` task output is never truncated — it's the bot's response to the user

Expansion is stateless — pressing Enter shows the full output inline, no scrollback modification.

#### Cancel Display

When the user presses `Ctrl+C` during execution (not at the prompt):

```
^C
⊘ Cancelling...

(current task finishes)

⊘ Cancelled. 2 of 4 tasks completed.
   Done: exec (fly launch), skill:aider (update fly.toml)
   Skipped: exec (fly deploy), msg (final response)
```

`Ctrl+C` at the prompt (not during execution) exits the REPL.

#### Spinner

While a task is running, the CLI shows a spinner animation on the task header line:

```
▶ [2/3] exec: fly deploy ⠋
```

The spinner replaces itself with the final status icon (`✓` or `✗`) when the task completes. Uses a standard braille spinner (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`), cycling every 80ms.

#### Terminal Capabilities

The renderer detects terminal capabilities at startup:

| Capability | Detection | Fallback |
|------------|-----------|----------|
| 256-color | `TERM` contains `256color` or `COLORTERM` is set | No colors |
| Unicode | `LC_ALL` / `LANG` contains `UTF-8` | ASCII icons |
| Width | `os.get_terminal_size()` | 80 columns |
| Interactive | `sys.stdout.isatty()` | No spinner, no truncation, no color (pipe-friendly) |

When stdout is not a TTY (piped), the CLI outputs plain text with no ANSI codes, no spinner, and no truncation. This makes `kiso --session dev | tee log.txt` work correctly.

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
