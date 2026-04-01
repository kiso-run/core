# CLI

The `kiso` command serves two purposes: interactive chat client and management tool.

## Chat Mode

```bash
kiso                                           # default session: hostname@user
kiso --session my-project                      # specific session
kiso --api http://remote:8333                  # remote server
kiso --quiet                                   # only show bot messages
```

## Single-Shot Mode

```bash
kiso msg "what is 2+2?"                        # send one message, print response, exit
kiso msg "list files" --quiet                  # quiet output
kiso msg "hello" --session dev                 # specific session
```

`kiso msg` sends a single message, polls for the response, prints it, and exits. Implicitly quiet when stdout is not a TTY (e.g. piped to another command). Useful for scripting and one-off questions.

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--instance NAME` / `-i NAME` | implicit (if one instance) | Which bot instance to connect to |
| `--session SESSION` | `{hostname}@{user}` | Session identifier |
| `--api URL` | `http://localhost:{instance_port}` | Server URL (auto-set per instance) |
| `--quiet` / `-q` | off | Only show `msg` task content (hide decision flow) |

By default, the CLI shows the **full decision flow** — planning, task execution, review verdicts, replans, and bot messages. This is the primary way to understand what kiso is doing and why. Use `--quiet` to suppress everything except the final bot responses.

The API token is read from `~/.kiso/instances/{name}/config.toml`: the CLI always uses the token named `cli` from the `[tokens]` section.

### Default Session

If not specified, the session is: `{hostname}@{user}`

The user comes from the Linux user (`whoami`). Example: `laptop@marco`

### Behavior

1. Generates session ID (default or from `--session`)
2. Shows interactive prompt
3. On each user input:
   - POSTs to `/msg` on localhost (or `--api`) with `user` = Linux user
   - Polls `GET /status/{session}?after={last_task_id}` until plan completes
   - Renders each event as it arrives (see [Display Rendering](#display-rendering))
4. Loops until `Ctrl+C` or `exit`

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/status` | Server health + session info |
| `/sessions` | List your sessions |
| `/verbose-on` | Show full LLM input/output (messages sent and responses received) with beautified JSON |
| `/verbose-off` | Hide LLM details (default) |
| `/clear` | Clear the screen |
| `/exit` | Exit the REPL |

### Display Rendering

The CLI renders the full execution flow in real time. Every decision the system makes is visible to the user. The renderer reads task updates from `/status` polling and maps each event to a display element.

#### Display Modes

| Mode | Shows | When |
|------|-------|------|
| **Default** | Everything: plan goal, task progress, output, review verdicts, replans, learnings, bot messages | Always, unless `--quiet` |
| **Quiet** (`--quiet`) | Only `msg` task content — the bot's actual responses | When you don't care about internals |
| **Verbose** (`/verbose-on`) | Default + full LLM input/output in bordered panels | When debugging LLM interactions |

#### Verbose Mode

Toggle with `/verbose-on` and `/verbose-off` during a chat session. When enabled, LLM call panels show the full input/output for each LLM call in a task. Each panel shows:

- **Messages sent**: each message with its role label (`[system]`, `[user]`, etc.)
- **Thinking/reasoning** (if present): models that return reasoning content — via the API-level `reasoning_content` field (DeepSeek, OpenRouter) or `<think>`/`<thinking>` tags embedded in the response — get a separate `🤔 reasoning` section displayed in yellow between the input and the response. Thinking is extracted at the LLM layer (`kiso/text.py`) and stored in each call's usage entry, so it is available both in verbose panels and in the messenger output.
- **Response received**: the full LLM response (with thinking tags already stripped), with JSON responses pretty-printed. Curator output uses a human-readable format showing verdict, fact, entity (name + kind), and tags for each evaluation instead of raw JSON.

Example verbose flow for an exec task:

```
▶ [1/3] exec: check file exists  translating ⠋
  ┌─ translator → deepseek-v3 (300→45) 14:32:05 ┐
  │ [system] You translate natural language...   │
  │ [user] ## Task\ncheck file exists...         │
  │ [response] ls -la requirements.txt           │
  └──────────────────────────────────────────────┘
  $ ls -la requirements.txt
▶ [1/3] exec: check file exists  running ⠋
▶ [1/3] exec: check file exists  reviewing ⠋
  ┌─ reviewer → deepseek-v3 (350→60) 14:32:08 ─┐
  │ ...                                          │
  └──────────────────────────────────────────────┘
✓ [1/3] exec: check file exists
  ┊ -rw-r--r-- 1 root root 245 ...
  ✓ review: ok
```

**Panel ordering invariant:** Verbose panels must always appear in strict IN→OUT→IN→OUT order. Each LLM call shows its input (IN) panel first, then after the call completes, the output (OUT) panel. When multiple roles run sequentially (e.g. briefer → messenger), the briefer's LLM calls are flushed to the database before the messenger starts — this ensures the CLI renders briefer IN→briefer OUT→messenger IN→messenger OUT, never two consecutive IN panels. This flush happens via the `on_briefer_done` callback in both the main plan loop (`_handle_msg_task`) and the fast path (`_fast_path_chat`).

This is useful for debugging prompt issues, verifying what context the LLM receives, and inspecting structured output. The data is fetched via `GET /status/{session}?verbose=true` — the default `/status` response omits message/response data to keep payloads small. The verbose endpoint is also available for API consumers directly.

> **M41 — spinner before the plan exists**: The planning spinner activates as soon as `worker_running=True`, even before the plan is written to the database. This covers the classifier + planner LLM calls (typically 4–15 s) that previously left the CLI appearing frozen.

> **M41 — fast task visibility**: The poll interval is 160 ms (`_POLL_EVERY = 2` × 80 ms loop), reduced from 480 ms. Most tasks are now observed in `"running"` state before completing, so per-task spinners appear correctly.

#### Visual Elements

**Colors** (256-color terminal, graceful fallback to no-color if unsupported):

| Element | Color | Purpose |
|---------|-------|---------|
| Plan goal | **bold cyan** | Distinguishes the high-level objective |
| Task header (`exec`, `tool`, `search`) | **yellow** | Work in progress |
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
| tool task | `⚡` | `!` | Tool running |
| search task | `🔍` | `S` | Web search running |
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
────────────────────────────────────────────────────────────
  1. [exec]    Run fly launch to initialize the app
  2. [tool]    Update fly.toml to add health check endpoint
  3. [exec]    Deploy the app to fly.io
────────────────────────────────────────────────────────────

▶ [1/3] exec: Run fly launch to initialize the app
  $ fly launch --no-deploy --name dev-app
  ┊ Scanning source code... Detected a FastAPI app
  ┊ Creating app "dev-app" in organization "personal"
  ┊ Wrote config file fly.toml
  ✓ review: ok  ⟨430→85⟩

⚡ [2/3] tool:aider: Update fly.toml to add health check endpoint
  ┊ Edited fly.toml: added [[services.http_checks]] section
  ✓ review: ok  ⟨310→62⟩

▶ [3/3] exec: Deploy the app to fly.io
  $ fly deploy
  ┊ ==> Building image with Docker
  ┊ ...
  ┊ Error: failed to fetch an image or build from source: no such file: Dockerfile
  ✗ review: replan — "No Dockerfile found. Need to create one first."  ⟨520→95⟩

↻ Replan: Create Dockerfile then deploy (3 tasks)
────────────────────────────────────────────────────────────
  1. [exec]    Create a Dockerfile
  2. [exec]    Deploy to fly.io
  3. [msg]     Report deployment results
────────────────────────────────────────────────────────────

▶ [1/3] exec: Create a Dockerfile
  $ cat > Dockerfile << 'EOF' ...
  ✓ review: ok  ⟨280→45⟩

▶ [2/3] exec: Deploy to fly.io
  $ fly deploy
  ┊ ==> Building image
  ┊ ==> Pushing image
  ┊ ==> Monitoring deployment
  ┊ v0 deployed successfully
  ✓ review: ok  ⟨410→70⟩

💬 [3/3] msg
Bot: Deployed to fly.io. The app is live at https://dev-app.fly.dev.
     I had to create a Dockerfile first since one wasn't present.
     The health check is configured at /health.
⟨620→150⟩
────────────────────────────────────────────────────────────
⟨ 4,521 in → 1,203 out │ deepseek/deepseek-v3.2 ⟩

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
✗ review: replan — "Still can't connect to the database"  ⟨380→72⟩
⊘ Max replans reached (3). Giving up.

💬 msg
Bot: I wasn't able to complete the task. After 3 attempts, the database
     connection keeps failing. Please check that PostgreSQL is running
     and the connection string is correct.
⟨540→120⟩
────────────────────────────────────────────────────────────
```

#### Output Truncation

Long task output (exec stdout/stderr, tool output) is truncated in the terminal to keep the display readable:

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
   Done: exec (fly launch), tool:aider (update fly.toml)
   Skipped: exec (fly deploy), msg (final response)
```

`Ctrl+C` at the prompt (not during execution) exits the REPL.

#### Plan Detail

When a plan is created, the CLI shows a numbered list of all planned tasks before execution starts:

```
◆ Plan: Install dependencies (3 tasks)
────────────────────────────────────────────────────────────
  1. [exec]    Check if pyproject.toml exists
  2. [exec]    Run uv sync
  3. [msg]     Summarize results
────────────────────────────────────────────────────────────
```

#### Translated Commands

For `exec` tasks, the CLI shows the actual shell command translated from the natural-language description:

```
▶ [1/3] exec: List all Python files in the project
  $ find . -name "*.py" -type f
  ┊ ./main.py
  ┊ ./utils.py
```

The `$` line shows the command produced by the exec translator. This makes it clear what shell command was actually run.

#### Search Tasks

For `search` tasks, the CLI shows the search query and results:

```
🔍 [1/2] search: best SEO agencies in Milan  searching ⠋
✓ [1/2] search: best SEO agencies in Milan
  ┊ {"results": [...], "summary": "..."}
  ✓ review: ok
  searcher     200→800  gemini-2.5-flash-lite
  reviewer     350→60   deepseek-v3
```

Search tasks use the built-in searcher role (`google/gemini-2.5-flash-lite:online` by default) for web lookups. If the `search` tool is installed, the planner prefers it for bulk queries (>10 results) since dedicated search APIs (Brave, Serper) are cheaper per result.

#### Token Usage

Token usage is tracked at two levels:

**Per-step**: after each task completes, the CLI shows a compact token count:

```
  ✓ review: ok  ⟨430→85⟩
```

For `msg` tasks (no review line), the count appears after the bot message:

```
Kiso: Deployed successfully.
⟨620→150⟩
────────────────────────────────────────────────────────────
```

The per-step count includes all LLM calls for that task (exec translator + reviewer for exec/tool tasks, searcher + reviewer for search tasks, messenger for msg tasks).

**Grand total**: after plan completion, the CLI shows the full summary:

```
⟨ 2,410 in → 545 out │ deepseek/deepseek-v3.2 ⟩
```

Shows total input tokens, output tokens, and the model used. Includes planner, all task steps, and post-plan processing (curator, summarizer). Hidden in quiet mode. Uses ASCII fallback (`< ... >`) on non-Unicode terminals.

#### Spinner

While a task is running, the CLI shows a spinner animation on the task header line with the current phase:

```
▶ [1/3] exec: check file exists  translating ⠋
▶ [1/3] exec: check file exists  running ⠋
▶ [1/3] exec: check file exists  reviewing ⠋
🔍 [2/3] search: best SEO agencies  searching ⠋
💬 [3/3] msg: present results  composing ⠋
```

The phase label shows what the worker is currently doing:

| Phase | Shown for | Meaning |
|-------|-----------|---------|
| `translating` | exec | Exec translator LLM converting task detail to shell command |
| `running` | exec, tool | Shell command or tool subprocess executing |
| `reviewing` | exec, tool, search | Reviewer LLM checking task output |
| `searching` | search | Searcher LLM performing web search |
| `composing` | msg | Messenger LLM generating user-facing message |

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

## Configuration Management

### kiso config

Manage runtime settings without editing config.toml manually.

```bash
kiso config set KEY VALUE    # Set a setting (admin, hot-reloads)
kiso config get KEY          # Show current value
kiso config list             # Show all settings
```

Examples:

```bash
kiso config set bot_persona "a sarcastic Neapolitan professor"
kiso config set dream_enabled false
kiso config get bot_name
kiso config list
```

Changes are written to `config.toml` and hot-reloaded automatically. Admin access required for all write operations.

## Tool Management

Only admins can install, update, and remove tools.

```bash
kiso tool search [query]                       # search official tools from registry
kiso tool install <name>                       # official: resolves from kiso-run org
kiso tool install <git-url>                    # unofficial: clone from any git URL
kiso tool install <git-url> --name foo         # unofficial with custom name
kiso tool install <git-url> --no-deps          # skip deps.sh execution
kiso tool update <name>                        # git pull + deps.sh + uv sync
kiso tool update all                           # update all installed tools
kiso tool remove <name>
kiso tool list                                 # list installed tools
```

### Search

Searches the official registry (`registry.json` in the core repo). Matches by name first, then by description:

```bash
$ kiso tool search
  search  — Web search with multiple backends (Brave, Serper)
  aider   — Code editing, refactoring, bug fixes using aider

$ kiso tool search code
  aider   — Code editing, refactoring, bug fixes using aider
```

If no results match but the other plugin type has matches, a cross-type hint is shown:

```
$ kiso connector search browser
No connectors found.
Did you mean `kiso tool search browser`? Found in tools: browser
```

### Install Flow

See [tools.md — Install Flow](tools.md#install-flow) for the full 10-step sequence (includes `.installing` marker to prevent discovery during install).

### Naming

| Source | Installed as |
|---|---|
| `kiso tool install search` | `~/.kiso/instances/{instance}/tools/search/` |
| `kiso tool install git@github.com:foo/bar.git` | `~/.kiso/instances/{instance}/tools/github-com_foo_bar/` |
| `kiso tool install <url> --name custom` | `~/.kiso/instances/{instance}/tools/custom/` |

URL to name: see [tools.md — Naming Convention](tools.md#naming-convention) for the full algorithm.

## Connector Management

Only admins can install, update, and remove connectors.

```bash
kiso connector search [query]                  # search official connectors from registry
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
kiso connector run discord                     # start as daemon
kiso connector stop discord                    # stop the daemon
kiso connector status discord                  # check if running
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

## Reset / Cleanup

Only admins can run reset commands. All commands require `--yes` (or `-y`) to skip interactive confirmation.

```bash
kiso reset session [name]     # clear one session (default: current)
kiso reset knowledge          # clear all facts, learnings, pending items
kiso reset all                # clear all sessions + knowledge + audit + history
kiso reset factory            # wipe everything, reinitialize (keeps config.toml + .env)
```

Four levels, from lightest to heaviest:

| Level | DB | Filesystem | Keeps |
|-------|-----|------------|-------|
| `session` | messages, plans, tasks, facts, learnings, pending for that session; session row | `sessions/{name}/` | everything else |
| `knowledge` | facts, learnings, pending (all rows) | nothing | sessions, config, tools |
| `all` | all rows in all tables | `sessions/`, `audit/`, `.chat_history` | config.toml, .env, tools, connectors |
| `factory` | store.db deleted entirely | `sessions/`, `audit/`, `tools/`, `connectors/`, `roles/`, `reference/`, `sys/`, `.chat_history`, `server.log` | config.toml, .env, docker-compose.yml |

### Architecture

`kiso reset` opens the database directly with sync `sqlite3` — no API call needed. This works because:

- SQLite WAL mode handles concurrent access safely
- The server may not be running when you want to reset
- Same pattern as `kiso env` (direct file) and `kiso tool` (direct filesystem)

After `kiso reset factory`, the host wrapper automatically restarts the container so the server reinitializes with a fresh database.

### Examples

```bash
# Reset your current session (will prompt for confirmation)
kiso reset session

# Reset a specific session without prompting
kiso reset session dev-backend --yes

# Clear all accumulated knowledge (facts, learnings, pending items)
kiso reset knowledge -y

# Start completely fresh (keeps only config.toml and .env)
kiso reset factory --yes
```

## User Management

Only admins can add, remove, or manage users.

```bash
kiso user list                                                    # list all users with role, tools, aliases
kiso user list --json                                             # machine-readable JSON output
kiso user add <user> --role admin|user                        # add an admin (no tools needed)
kiso user add <user> --role user --skills "*"                  # add a user with all tools
kiso user add <user> --role user --skills "search,aider"       # add a user with specific tools
kiso user add <user> --role user --skills "*" \
    --alias discord:bob#1234 --alias slack:U0123456           # add with connector aliases
kiso user edit <user> --role admin                            # change role in-place
kiso user edit <user> --skills "read,write"                    # change tools in-place
kiso user remove <user>                                       # remove a user
kiso user alias <user> --connector discord --id "bob#1234"    # set a connector alias
kiso user alias <user> --connector discord --remove           # remove a connector alias
```

Changes are written to `config.toml` and the running server is hot-reloaded automatically (via `POST /admin/reload-config`). No restart needed.

Pass `--no-reload` to any write command (`add`, `edit`, `remove`, `alias`) to skip the reload — useful during initial setup before the server is running.

**Note**: `tomli_w` rewrites the entire `config.toml` without preserving comments. Values are preserved faithfully, but inline comments are removed on the first user management operation.

### `kiso user add` options

| Flag | Required | Description |
|------|----------|-------------|
| `--role admin\|user` | yes | User role. No default — must be explicit. |
| `--skills` | if `role=user` | `"*"` for all tools, or comma-separated names (e.g. `"search,aider"`). Ignored for admins. |
| `--alias CONNECTOR:ID` | no | Connector alias in `connector:platform_id` format. Repeatable. |
| `--no-reload` | no | Skip hot-reload after writing config. |

### `kiso user edit` options

| Flag | Required | Description |
|------|----------|-------------|
| `--role admin\|user` | at least one of `--role`/`--skills` | New role. |
| `--skills` | at least one of `--role`/`--skills` | New tools: `"*"` or comma-separated names. |
| `--no-reload` | no | Skip hot-reload after writing config. |

**Error handling:**

| Situation | Output |
|-----------|--------|
| User fails `NAME_RE` validation | `error: invalid user '...'` |
| User already exists (`add`) | `error: user '...' already exists` |
| User does not exist (`edit`/`remove`/`alias`) | `error: user '...' does not exist` |
| `--role` omitted on `add` | `error: --role must be 'admin' or 'user'` |
| `role=user` without `--skills` | `error: --skills required for role=user` |
| Both `--role` and `--skills` omitted on `edit` | `error: at least one of --role or --skills must be provided` |
| Alias in wrong format | `error: alias '...' must be in 'connector:platform_id' format` |
| Removing/demoting last admin | `error: cannot remove/demote the last admin` |

## Deploy Secret Management

Only admins can manage deploy secrets.

```bash
kiso env set KISO_TOOL_SEARCH_API_KEY sk-...   # set a deploy secret
kiso env get KISO_TOOL_SEARCH_API_KEY          # show value
kiso env list                                  # list all deploy secrets (names only)
kiso env delete KISO_TOOL_SEARCH_API_KEY       # remove a deploy secret
kiso env reload                                # hot-reload .env without restart
```

Secrets are stored in `~/.kiso/instances/{name}/.env` and loaded into the process environment. `kiso env reload` calls `POST /admin/reload-env` to hot-reload without restarting the server. See [security.md — Deploy Secrets](security.md#deploy-secrets).

## Token Usage Statistics

```bash
kiso stats                              # usage for last 30 days, grouped by model
kiso stats --since 7                    # last 7 days
kiso stats --session alice              # filter to a specific session
kiso stats --by session                 # group by session instead of model
kiso stats --by role                    # group by LLM role (planner, reviewer, …)
kiso stats --all                        # loop over all instances (wrapper command)
```

`kiso stats` calls `GET /admin/stats` (admin token required) and prints a formatted table:

```
Token usage — last 30 days  (by model)

  model                    calls    input   output   est. cost
  ─────────────────────────────────────────────────────────────
  google/gemini-flash        142   1 234 k    456 k      $0.42
  anthropic/claude-sonnet     23      98 k     34 k      $1.81
  ollama/llama3                5       2 k      1 k         —
  ─────────────────────────────────────────────────────────────
  total                      170   1 334 k    491 k      $2.23
```

- Token counts use space as thousands separator + `k` suffix (e.g. `1 234 k`).
- `est. cost` is computed from a built-in price table (substring match on model name). The column is omitted entirely if no model in the table has a known price.
- `—` means the model is not in the price table.
- `--all` (wrapper only): iterates all instances in `instances.json` and prints a `── name ──` header before each. Instances that are not running show a `(not running)` message instead of an error.

The API token must have the `cli` key in `config.toml`, and the Linux user must be configured as admin. See [security.md — Roles](security.md#roles).

**Error handling:**

| Situation | Output |
|-----------|--------|
| No `cli` token in `config.toml` | `error: no 'cli' token in config.toml` |
| Server not reachable | `error: cannot connect to http://localhost:PORT` |
| User is not admin | `error: 403 — Admin access required` |

### REPL `/stats`

Inside the interactive REPL, `/stats` fetches the last 7 days of usage for the **current session** only and prints a compact table inline. If the server is unreachable or the user lacks admin access, a brief inline error is shown instead.

## Shell Completion

```bash
kiso completion bash    # print bash completion script
kiso completion zsh     # print zsh completion script
```

To install permanently:

```bash
# bash
kiso completion bash >> ~/.bash_completion

# zsh
kiso completion zsh > "${fpath[1]}/_kiso"
```

Or to activate for the current shell session only:

```bash
source <(kiso completion bash)
```

The wrapper (`kiso-host.sh`) fetches the completion script from inside a running Docker container via `docker exec`. If no instance is running, it falls back to the system-installed completion files. The completion scripts support:

- All top-level commands including `stats`, `completion`, `instance`, etc.
- `kiso stats --session` and `kiso reset session` → tab-complete session names from the active instance's DB
- `kiso instance explore SESSION` → tab-complete session names
- `kiso tool search` and `kiso connector search` → tab-complete from locally installed plugin names
- `kiso tool update`, `kiso tool remove`, `kiso connector update`, `kiso connector remove|run|stop|status` → tab-complete from locally installed names
- `kiso --instance NAME` → detected from command line; completion automatically queries that instance's DB
- When multiple instances exist and no `--instance` is specified, session completion is silently skipped

## Version Information

```bash
kiso version           # print version string
kiso version --stats   # version + LOC breakdown per area
```

`kiso version --stats` counts non-empty, non-comment Python lines (`.py` files only) across three areas:

```
kiso 0.1.0

  core    4 950 loc   (kiso/)
  cli     2 323 loc   (cli/)
  tests  19 681 loc   (tests/)
  ──────────────────
  total  26 954 loc
```

Lines that are blank or start with `#` (after stripping whitespace) are excluded. Inline comments (`x = 1  # note`) count as LOC.

## Notes

- Chat mode is a thin HTTP wrapper — all intelligence lives in the server.
- Works against a remote server (`--api`) — useful for running kiso on a VPS.
- Instance management (`kiso instance *`) and installation (`install.sh`) are documented in [docker.md](docker.md).

## Code Organization

The CLI lives in a root-level `cli/` package, separate from `kiso/` (the server/bot code). This boundary keeps the bot line count clean and makes it clear that the CLI is a client of the server, not part of it.

```
cli/
├── __init__.py    ← entry point, argument parsing, REPL loop, /verbose commands
├── connector.py   ← kiso connector subcommands (install, update, remove, run, stop, status)
├── env.py         ← kiso env subcommands (set, get, list, delete, reload)
├── plugin_ops.py  ← shared utilities for tool and connector management
├── render.py      ← terminal renderer (task display, markdown, spinner, colors)
├── reset.py       ← kiso reset subcommands (session, knowledge, all, factory)
├── session.py     ← kiso sessions subcommand
├── tool.py        ← kiso tool subcommands (install, update, remove, list, search)
└── user.py        ← kiso user subcommands (list, add, remove, alias)
```

Server-side code (`kiso/`) has no dependency on `cli/`. The CLI depends on `kiso/` only for config path constants and store access (direct SQLite for `reset` and `env` commands).
