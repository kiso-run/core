# Configuration

## What Goes Where

**TOML** for static stuff you edit by hand: providers, models, tokens, users, settings.

**SQLite** for everything dynamic: sessions, messages, tasks, facts, learnings, pending items, published files.

Each instance has its own data directory. With an instance named `jarvis`:

```
~/.kiso/instances/jarvis/
├── config.toml          # static, human-readable, versionable
├── .env                 # deploy secrets (managed via `kiso env`)
├── store.db             # dynamic, machine-managed
└── audit/               # LLM call logs, task execution logs
```

See [docker.md](docker.md) for the full directory layout and instance registry.

## First run

On first start, if `config.toml` does not exist in the instance directory, kiso writes a complete template with all fields pre-set to their defaults, then exits with instructions:

```
Config created at KISO_DIR/config.toml
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

[users.luca]
role = "user"

[models]
briefer      = "google/gemini-2.5-flash"
classifier   = "google/gemini-2.5-flash"
planner      = "deepseek/deepseek-v3.2"
reviewer     = "google/gemini-2.5-flash-lite"
curator      = "google/gemini-2.5-flash"
worker       = "deepseek/deepseek-v3.2"
summarizer   = "google/gemini-2.5-flash-lite"
paraphraser  = "google/gemini-2.5-flash-lite"
messenger    = "deepseek/deepseek-v3.2"
consolidator = "google/gemini-2.5-flash-lite"
mcp_sampling = "google/gemini-2.5-flash"

[settings]
# --- conversation ---
context_messages          = 5        # recent messages sent to planner
summarize_threshold       = 30       # message count before summarizer runs
summarize_messages_limit  = 100      # max messages sent to summarizer LLM per run
bot_name                  = "Kiso"
bot_persona               = "a friendly and knowledgeable assistant"

# --- knowledge / memory ---
knowledge_max_facts           = 50
fact_decay_days               = 7
fact_decay_rate               = 0.1
fact_archive_threshold        = 0.3
fact_consolidation_min_ratio  = 0.3
consolidation_enabled         = true    # periodic knowledge consolidation
consolidation_interval_hours  = 24      # hours between consolidation runs
consolidation_min_facts       = 20      # minimum facts to trigger a consolidation run

# --- planning ---
max_replan_depth          = 5
max_validation_retries    = 3
max_llm_retries           = 3        # retries on LLM HTTP/stall errors (per call)
max_plan_tasks            = 20
planner_fallback_model    = "minimax/minimax-m2.7"          # fallback when primary planner model fails

# --- execution ---
classifier_timeout        = 30       # seconds for classifier LLM call; falls back to planner on timeout
llm_timeout               = 600      # seconds; timeout for all LLM calls
stall_timeout             = 60       # seconds; SSE stall detection per chunk
max_output_size           = 1048576  # max chars per task output (0 = unlimited)
max_worker_retries        = 2
external_url              = ""       # public URL for file download links (e.g. "http://1.2.3.4:8334")

# --- resource limits ---
max_memory_gb             = 4        # container RAM limit
max_cpus                  = 2        # container CPU limit
max_disk_gb               = 32       # app-level disk limit
max_pids                  = 512      # container PID limit

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

# --- briefer (context intelligence layer) ---
briefer_enabled                    = true
briefer_mcp_method_filter_threshold = 10   # when the catalog of eligible MCP methods exceeds this count, delegate final selection to the briefer
briefer_skill_filter_threshold      = 10   # same threshold, for skills; below, pass all eligible skills through unfiltered

# --- MCP runtime ---
mcp_session_idle_timeout           = 1800  # seconds; shut down a per-session MCP client idle this long (60-7200)
mcp_max_session_clients_per_server = 32    # LRU bound on per-session clients for a single MCP server (1-256)
mcp_warmup_concurrency             = 3     # parallelism for daemon-boot MCP catalog warm-up (1-16)
mcp_warmup_deadline_s              = 10    # total wall-clock deadline for warm-up to complete (1-120)
mcp_sampling_enabled               = true  # allow MCP servers to call back into kiso via sampling/createMessage

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
| `[users.<name>.mcp]` | Optional per-user MCP method filter (role-scoped visibility). See [mcp.md](mcp.md#per-user-role-filters). |
| `[users.<name>.skills]` | Optional per-user skill filter (role-scoped visibility). See [skills.md](skills.md). |
| `[models]` | All 11 roles required: `briefer`, `classifier`, `planner`, `reviewer`, `curator`, `worker`, `summarizer`, `paraphraser`, `messenger`, `consolidator`, `mcp_sampling`. The `classifier` only returns "plan" or "chat" — use a fast/cheap model. The `mcp_sampling` role fulfils `sampling/createMessage` requests coming back from MCP servers (see [mcp.md](mcp.md#sampling)). |
| `[settings]` | All fields required. See table below. |

### Settings reference

| Field | Default | Description |
|---|---|---|
| `users.*.aliases.*` | (none) | Platform identity per connector. Key = connector/token name, value = platform user. See [security.md](security.md). |
| `context_messages` | `5` | Number of recent raw messages sent to the planner. |
| `summarize_threshold` | `30` | Summarizer triggers when raw message count reaches this value. |
| `summarize_messages_limit` | `100` | Max messages sent to summarizer LLM per run. |
| `bot_name` | `"Kiso"` | Name used by the messenger when referring to itself. |
| `bot_persona` | `"a friendly and knowledgeable assistant"` | Messenger personality. Templated into messenger.md as `{bot_persona}`. Change with `kiso config set bot_persona "value"`. |
| `knowledge_max_facts` | `50` | Max global facts before consolidation. |
| `fact_decay_days` | `7` | Facts not used in this many days lose `fact_decay_rate` confidence per post-plan cycle. |
| `fact_decay_rate` | `0.1` | How much confidence is subtracted per decay cycle (0.0–1.0). |
| `fact_archive_threshold` | `0.3` | Facts with confidence below this are moved to `facts_archive` and removed from active context. |
| `fact_consolidation_min_ratio` | `0.3` | Minimum fraction of facts that must survive consolidation. If the LLM returns fewer than this fraction, consolidation is aborted and the original facts are kept. |
| `consolidation_enabled` | `true` | Enable periodic knowledge consolidation. Reviews and deduplicates facts on a schedule. |
| `consolidation_interval_hours` | `24` | Hours between consolidation runs. |
| `consolidation_min_facts` | `20` | Minimum number of facts required to trigger a consolidation run. |

| `max_replan_depth` | `5` | Max replan cycles per original message. |
| `max_validation_retries` | `3` | Max retries when planner returns structurally valid JSON that fails semantic validation. |
| `max_llm_retries` | `3` | Max retries on LLM HTTP errors or SSE stalls per call. |
| `max_plan_tasks` | `20` | Max tasks per plan. Plans exceeding this fail validation. See [security.md — Plan Task Limit](security.md#plan-task-limit). |
| `planner_fallback_model` | `"minimax/minimax-m2.7"` | Fallback model when primary planner model exhausts retries. |
| `classifier_timeout` | `30` | Seconds before classifier LLM call is cancelled. Falls back to planner path on timeout. |
| `llm_timeout` | `600` | Seconds before any LLM call is cancelled. Also used for graceful shutdown per worker. |
| `stall_timeout` | `60` | Seconds without SSE data before declaring a stall. Triggers model switch to fallback. |
| `max_output_size` | `1048576` | Max characters of stdout/stderr per exec task before truncation (0 = unlimited). See [security.md — Output Size Limits](security.md#output-size-limits). |
| `max_worker_retries` | `2` | Max worker-level retries per exec/mcp task before escalating to a full replan. |
| `external_url` | `""` | Public URL for published file download links. Set by installer when public network is chosen. |
| `max_memory_gb` | `4` | Container RAM limit (applied via docker run/update). |
| `max_cpus` | `2` | Container CPU limit (applied via docker run/update). |
| `max_disk_gb` | `32` | App-level disk limit (applied immediately). |
| `max_pids` | `512` | Container PID limit (applied via docker run/update). |
| `max_llm_calls_per_message` | `200` | Budget cap on LLM calls per user message. Prevents runaway replan loops. |
| `max_message_size` | `65536` | Max bytes for POST /msg content. Requests exceeding this return 413. See [security.md — Input Validation](security.md#input-validation). |
| `max_queue_size` | `50` | Max queued messages per session before backpressure (429). See [security.md — Queue Backpressure](security.md#queue-backpressure). |
| `host` | `"0.0.0.0"` | Server bind address. |
| `port` | `8333` | Server port. |
| `worker_idle_timeout` | `300` | Seconds before idle worker shuts down. |
| `fast_path_enabled` | `true` | Skip planner for conversational messages (classifier decides). |
| `briefer_enabled` | `true` | LLM-based context selection for each pipeline stage. When disabled, all context is passed to every LLM call. |
| `briefer_mcp_method_filter_threshold` | `10` | When the catalog of eligible MCP methods exceeds this count, the briefer selects the final subset for the planner. Below it, the planner sees them all. |
| `briefer_skill_filter_threshold` | `10` | Same threshold, applied to skills. Keeps the planner prompt bounded without shutting off small-catalog scenarios. |
| `mcp_session_idle_timeout` | `1800` | Seconds before a per-session MCP client is shut down for inactivity. Range 60-7200. |
| `mcp_max_session_clients_per_server` | `32` | LRU cap on the number of per-session clients kept open for a single MCP server. Range 1-256. |
| `mcp_warmup_concurrency` | `3` | Parallelism for the daemon-boot MCP catalog warm-up. Range 1-16. |
| `mcp_warmup_deadline_s` | `10` | Wall-clock deadline for warm-up. Range 1-120. Servers that do not respond in time are retried on first demand. |
| `mcp_sampling_enabled` | `true` | When true, MCP servers may call back into kiso via `sampling/createMessage` using the `mcp_sampling` model role. |
| `webhook_allow_list` | `[]` | IPs exempt from webhook SSRF validation (e.g. `["127.0.0.1"]` for local connectors). See [security.md — Webhook Validation](security.md#7-webhook-validation). |
| `webhook_require_https` | `true` | Reject plain `http://` webhook URLs. Set to `false` for local development. |
| `webhook_secret` | `""` | HMAC-SHA256 secret for webhook signatures. Empty = no signature. |
| `webhook_max_payload` | `1048576` | Max webhook payload bytes before content truncation. |

## Tokens

Each client gets its own named token. The token name identifies the connector for alias resolution and is logged on each call. Revoking = remove from config, restart. Generate with `openssl rand -hex 32` or similar. See [security.md — API Authentication](security.md#2-api-authentication).

## Providers

All providers are OpenAI-compatible HTTP endpoints. Adding a provider = adding a section to config. The API key is read from the `OPENROUTER_API_KEY` environment variable (shared across all providers).

```toml
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[providers.ollama]
base_url = "http://localhost:11434/v1"
```

- `base_url`: **required**. No implicit default.
- API key: set `OPENROUTER_API_KEY` in `~/.kiso/instances/{name}/.env`. Optional for local providers (e.g. Ollama).

**Structured output requirement**: Planner, Reviewer, and Curator require `response_format` with strict `json_schema`. If the provider doesn't support it, the call fails with a clear error — no fallback. Worker, Summarizer, and Paraphraser produce free-form text and work with any provider.

## Model Routing

Model strings use `:` to specify a non-default provider. No `:` means the first listed provider.

```
z-ai/glm-4.7                      → first provider, model "z-ai/glm-4.7"
perplexity/sonar                 → first provider, model "perplexity/sonar"
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
aliases.discord = "anna_dev"

[users.luca]
role = "user"

# Optional per-user capability filters (role-scoped visibility):
[users.luca.mcp]
# allow = ["filesystem.*", "playwright.*"]
[users.luca.skills]
# allow = ["code-review", "browser-recipes"]
```

- **`role`**: `"admin"` (unrestricted exec, package management, full capability catalog) or `"user"` (sandboxed exec, capability visibility optionally narrowed via `[users.<name>.mcp]` / `[users.<name>.skills]`).
- **`aliases.*`**: maps platform identities to this Linux user. Key = connector/token name, value = platform user.
- **`[users.<name>.mcp]`**: optional MCP method filter. See [mcp.md](mcp.md#per-user-role-filters).
- **`[users.<name>.skills]`**: optional skill filter. See [skills.md](skills.md).

User identifiers are Linux users. CLI uses `$(whoami)` directly; connectors pass platform identity, resolved via aliases. See [security.md — User Identity](security.md#3-user-identity) for the full resolution flow.

### Hooks

Pre/post execution hooks for custom validation, logging, or blocking.
See [hooks.md](hooks.md) for details.

```toml
[[hooks.pre_exec]]
command = "/path/to/validator.sh"
blocking = true

[[hooks.post_exec]]
command = "/path/to/logger.sh"
```
