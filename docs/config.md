# Configuration

## What Goes Where

**TOML** for static stuff you edit by hand: providers, models, tokens, users, settings.

**SQLite** for everything dynamic: sessions, messages, tasks, facts, learnings, pending items, published files.

```
~/.kiso/
├── config.toml          # static, human-readable, versionable
├── .env                 # deploy secrets (managed via `kiso env`)
├── store.db             # dynamic, machine-managed
└── audit/               # LLM call logs, task execution logs
```

## Minimal config.toml

```toml
[tokens]
cli = "your-secret-token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[users.marco]
role = "admin"
```

If `[tokens]`, `[providers]`, or `[users]` are missing, kiso refuses to start and tells you what's missing.

## Full config.toml

All fields:

```toml
[tokens]
cli = "your-secret-token"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

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
planner = "minimax/minimax-m2.5"
reviewer = "deepseek/deepseek-v3.2"
curator = "deepseek/deepseek-v3.2"
worker = "deepseek/deepseek-v3.2"
summarizer = "deepseek/deepseek-v3.2"

[settings]
context_messages = 7
summarize_threshold = 30
knowledge_max_facts = 50
max_replan_depth = 3
max_validation_retries = 3
max_output_size = 1048576
exec_timeout = 120
worker_idle_timeout = 300
host = "0.0.0.0"
port = 8333
# webhook_allow_list = ["127.0.0.1", "::1"]
# webhook_require_https = true
# webhook_secret = ""
# webhook_max_payload = 1048576
```

### Required Fields

| Field | Description |
|---|---|
| `[tokens]` | At least one named token. Each client (CLI, connector) uses its own token. |
| `[providers]` | At least one provider with `base_url`. |
| `providers.*.base_url` | Required. No implicit default. |
| `[users]` | At least one user. Each user has a `role` (`admin` or `user`). |
| `users.*.role` | Required. `"admin"` or `"user"`. |
| `users.*.skills` | Required for `user` role. `"*"` for all skills, or a list of skill names. Ignored for admins (always all). If missing on a `user` role, kiso refuses to start. |

### Optional Fields (defaults)

| Field | Default | Description |
|---|---|---|
| `users.*.aliases.*` | (none) | Platform identity per connector. Key = connector/token name, value = platform username. See [security.md](security.md). |
| `models.planner` | `minimax/minimax-m2.5` | Requires structured output (`response_format` with `json_schema`). |
| `models.reviewer` | `deepseek/deepseek-v3.2` | Requires structured output. |
| `models.curator` | `deepseek/deepseek-v3.2` | Requires structured output. |
| `models.worker` | `deepseek/deepseek-v3.2` | Free-form text, no constraints. |
| `models.summarizer` | `deepseek/deepseek-v3.2` | Free-form text, also used by paraphraser. |
| `settings.context_messages` | `7` | Number of recent raw messages sent to the planner (see [llm-roles.md](llm-roles.md#planner)). |
| `settings.summarize_threshold` | `30` | Summarizer triggers when raw message count reaches this value |
| `settings.knowledge_max_facts` | `50` | Max global facts before consolidation |
| `settings.max_replan_depth` | `3` | Max replan cycles per original message |
| `settings.max_validation_retries` | `3` | Max retries when planner returns structurally valid JSON that fails semantic validation |
| `settings.max_output_size` | `1048576` | Max characters of stdout/stderr per exec or skill task before truncation (0 = unlimited). See [security.md — Output Size Limits](security.md#output-size-limits). |
| `settings.max_message_size` | `65536` | Max bytes for POST /msg content. Requests exceeding this return 413. See [security.md — Input Validation](security.md#input-validation). |
| `settings.max_queue_size` | `50` | Max queued messages per session before backpressure (429). See [security.md — Queue Backpressure](security.md#queue-backpressure). |
| `settings.max_plan_tasks` | `20` | Max tasks per plan. Plans exceeding this fail validation. See [security.md — Plan Task Limit](security.md#plan-task-limit). |
| `settings.exec_timeout` | `120` | Seconds before exec or skill subprocess is killed. Also used as timeout for post-plan LLM calls (curator, summarizer, fact consolidation), LLM HTTP calls, and graceful shutdown per worker. |
| `settings.worker_idle_timeout` | `300` | Seconds before idle worker shuts down |
| `settings.host` | `0.0.0.0` | Server bind address |
| `settings.port` | `8333` | Server port |
| `settings.webhook_allow_list` | `[]` | IPs exempt from webhook SSRF validation (e.g. `["127.0.0.1"]` for local connectors). See [security.md — Webhook Validation](security.md#7-webhook-validation). |
| `settings.webhook_require_https` | `true` | Reject plain `http://` webhook URLs. Set to `false` for local development. |
| `settings.webhook_secret` | `""` | HMAC-SHA256 secret for webhook signatures. Empty = no signature. |
| `settings.webhook_max_payload` | `1048576` | Max webhook payload bytes before content truncation. |

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
- API key: set `KISO_LLM_API_KEY` in `~/.kiso/.env`. Optional for local providers (e.g. Ollama).

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
