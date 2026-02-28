# Configuration

## What Goes Where

**TOML** for static stuff you edit by hand: providers, models, tokens, users, settings.

**SQLite** for everything dynamic: sessions, messages, tasks, facts, learnings, pending items, published files.

Each instance has its own data directory. With an instance named `jarvis`:

```
~/.kiso/instances/jarvis/
├── config.toml          # static, human-readable, versionable
├── .env                 # deploy secrets (managed via `kiso env`)
├── kiso.db              # dynamic, machine-managed
└── audit/               # LLM call logs, task execution logs
```

See [docker.md](docker.md) for the full directory layout and instance registry.

## First run

On first start, if `config.toml` does not exist in the instance directory, kiso writes a complete template with all fields pre-set to their defaults, then exits with instructions:

```
Config created at /root/.kiso/config.toml
  1. Set your token in [tokens]
  2. Configure [providers] and [users]
  3. Restart kiso
```

Edit the generated file: set your token, provider URL, and users. All other fields are already set to sensible defaults and can be left as-is or tuned.

**No hidden defaults.** All fields must be present in `config.toml`. If any are missing, kiso refuses to start and tells you exactly which ones. There are no silent fallbacks in the code.

## config.toml

All sections and fields are required (except `users.*.aliases`):

```toml
[tokens]
cli = "your-secret-token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

# [providers.ollama]
# base_url = "http://localhost:11434/v1"

[users.marco]
role = "admin"
aliases.discord = "Marco#1234"
aliases.telegram = "marco_tg"

[users.anna]
role = "user"
skills = "*"

[users.luca]
role = "user"
skills = ["search", "aider"]

[models]
planner     = "minimax/minimax-m2.5"
reviewer    = "deepseek/deepseek-v3.2"
curator     = "deepseek/deepseek-v3.2"
worker      = "deepseek/deepseek-v3.2"
summarizer  = "deepseek/deepseek-v3.2"
paraphraser = "deepseek/deepseek-v3.2"
messenger   = "deepseek/deepseek-v3.2"
searcher    = "perplexity/sonar"

[settings]
# --- conversation ---
context_messages          = 7        # recent messages sent to planner
summarize_threshold       = 30       # message count before summarizer runs
bot_name                  = "Kiso"

# --- knowledge / memory ---
knowledge_max_facts           = 50
fact_decay_days               = 7
fact_decay_rate               = 0.1
fact_archive_threshold        = 0.3
fact_consolidation_min_ratio  = 0.3

# --- planning ---
max_replan_depth          = 3
max_validation_retries    = 3
max_plan_tasks            = 20

# --- execution ---
exec_timeout              = 120      # seconds; also used for post-plan LLM calls
planner_timeout           = 60       # seconds for planner LLM call
max_output_size           = 1048576  # max chars per task output (0 = unlimited)
max_worker_retries        = 1

# --- limits ---
max_llm_calls_per_message = 200
max_message_size          = 65536    # bytes, POST /msg content
max_queue_size            = 50       # queued messages per session

# --- server ---
host                      = "0.0.0.0"
port                      = 8333
worker_idle_timeout       = 300

# --- fast path ---
fast_path_enabled         = true     # skip planner for conversational messages

# --- webhooks (only needed when using connector integrations) ---
webhook_allow_list        = []       # IPs exempt from SSRF check
webhook_require_https     = true
webhook_secret            = ""       # HMAC-SHA256 secret; empty = no signing
webhook_max_payload       = 1048576
```

### Required sections

| Section/Field | Description |
|---|---|
| `[tokens]` | At least one named token. Each client (CLI, connector) uses its own token. |
| `[providers]` | At least one provider with `base_url`. |
| `providers.*.base_url` | Required. No implicit default. |
| `[users]` | At least one user. Each user has a `role` (`admin` or `user`). |
| `users.*.role` | Required. `"admin"` or `"user"`. |
| `users.*.skills` | Required for `user` role. `"*"` for all skills, or a list of skill names. Ignored for admins (always all). |
| `[models]` | All 8 roles required: `planner`, `reviewer`, `curator`, `worker`, `summarizer`, `paraphraser`, `messenger`, `searcher`. |
| `[settings]` | All fields required. See table below. |

### Settings reference

| Field | Default | Description |
|---|---|---|
| `users.*.aliases.*` | (none) | Platform identity per connector. Key = connector/token name, value = platform username. See [security.md](security.md). |
| `context_messages` | `7` | Number of recent raw messages sent to the planner. |
| `summarize_threshold` | `30` | Summarizer triggers when raw message count reaches this value. |
| `bot_name` | `"Kiso"` | Name used by the messenger when referring to itself. |
| `knowledge_max_facts` | `50` | Max global facts before consolidation. |
| `fact_decay_days` | `7` | Facts not used in this many days lose `fact_decay_rate` confidence per post-plan cycle. |
| `fact_decay_rate` | `0.1` | How much confidence is subtracted per decay cycle (0.0–1.0). |
| `fact_archive_threshold` | `0.3` | Facts with confidence below this are moved to `facts_archive` and removed from active context. |
| `fact_consolidation_min_ratio` | `0.3` | Minimum fraction of facts that must survive consolidation. If the LLM returns fewer than this fraction, consolidation is aborted and the original facts are kept. |
| `max_replan_depth` | `3` | Max replan cycles per original message. |
| `max_validation_retries` | `3` | Max retries when planner returns structurally valid JSON that fails semantic validation. |
| `max_plan_tasks` | `20` | Max tasks per plan. Plans exceeding this fail validation. See [security.md — Plan Task Limit](security.md#plan-task-limit). |
| `exec_timeout` | `120` | Seconds before exec or skill subprocess is killed. Also used for post-plan LLM calls (curator, summarizer, fact consolidation), LLM HTTP calls, and graceful shutdown per worker. |
| `planner_timeout` | `60` | Seconds before a planner LLM call (initial plan or replan) is cancelled. Increase if using a slow model; decrease for faster failure feedback. |
| `max_output_size` | `1048576` | Max characters of stdout/stderr per exec or skill task before truncation (0 = unlimited). See [security.md — Output Size Limits](security.md#output-size-limits). |
| `max_worker_retries` | `1` | Max worker-level retries per exec/search task before escalating to a full replan. |
| `max_llm_calls_per_message` | `200` | Budget cap on LLM calls per user message. Prevents runaway replan loops. |
| `max_message_size` | `65536` | Max bytes for POST /msg content. Requests exceeding this return 413. See [security.md — Input Validation](security.md#input-validation). |
| `max_queue_size` | `50` | Max queued messages per session before backpressure (429). See [security.md — Queue Backpressure](security.md#queue-backpressure). |
| `host` | `"0.0.0.0"` | Server bind address. |
| `port` | `8333` | Server port. |
| `worker_idle_timeout` | `300` | Seconds before idle worker shuts down. |
| `fast_path_enabled` | `true` | Skip planner for conversational messages (classifier decides). |
| `webhook_allow_list` | `[]` | IPs exempt from webhook SSRF validation (e.g. `["127.0.0.1"]` for local connectors). See [security.md — Webhook Validation](security.md#7-webhook-validation). |
| `webhook_require_https` | `true` | Reject plain `http://` webhook URLs. Set to `false` for local development. |
| `webhook_secret` | `""` | HMAC-SHA256 secret for webhook signatures. Empty = no signature. |
| `webhook_max_payload` | `1048576` | Max webhook payload bytes before content truncation. |

## Tokens

Each client gets its own named token. The token name identifies the connector for alias resolution and is logged on each call. Revoking = remove from config, restart. Generate with `openssl rand -hex 32` or similar. See [security.md — API Authentication](security.md#2-api-authentication).

## Providers

All providers are OpenAI-compatible HTTP endpoints. Adding a provider = adding a section to config. The API key is read from the `KISO_LLM_API_KEY` environment variable (shared across all providers).

```toml
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[providers.ollama]
base_url = "http://localhost:11434/v1"
```

- `base_url`: **required**. No implicit default.
- API key: set `KISO_LLM_API_KEY` in `~/.kiso/instances/{name}/.env`. Optional for local providers (e.g. Ollama).

**Structured output requirement**: Planner, Reviewer, and Curator require `response_format` with strict `json_schema`. If the provider doesn't support it, the call fails with a clear error — no fallback. Worker, Summarizer, and Paraphraser produce free-form text and work with any provider.

## Model Routing

Model strings use `:` to specify a non-default provider. No `:` means the first listed provider.

```
minimax/minimax-m2.5             → first provider, model "minimax/minimax-m2.5"
deepseek/deepseek-v3.2           → first provider, model "deepseek/deepseek-v3.2"
ollama:llama3                    → provider "ollama", model "llama3"
```

`llm.py` splits on the first `:`, looks up the provider in config, and makes the call with the right `base_url` and `api_key`.

## Users

Whitelist-based. Only users in `[users]` trigger processing. Unknown users' messages are saved with `trusted=0` for context and audit but not processed.

```toml
[users.marco]
role = "admin"
aliases.discord = "Marco#1234"
aliases.email = "marco@example.com"

[users.anna]
role = "user"
skills = "*"
aliases.discord = "anna_dev"

[users.luca]
role = "user"
skills = ["search", "aider"]
```

- **`role`**: `"admin"` (unrestricted exec, package management, all skills) or `"user"` (sandboxed exec, allowed skills only).
- **`skills`**: which skills the planner can use for this user. `"*"` = all, or a list. Admins always have all skills regardless of this field.
- **`aliases.*`**: maps platform identities to this Linux user. Key = connector/token name, value = platform username.

User identifiers are Linux usernames. CLI uses `$(whoami)` directly; connectors pass platform identity, resolved via aliases. See [security.md — User Identity](security.md#3-user-identity) for the full resolution flow.
