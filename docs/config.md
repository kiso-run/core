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

**Required on first run:**

```toml
[tokens]
cli = "your-secret-token"

[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
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
api_key_env = "KISO_OPENROUTER_API_KEY"
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
planner = "moonshotai/kimi-k2.5"
reviewer = "moonshotai/kimi-k2.5"
curator = "moonshotai/kimi-k2.5"
worker = "deepseek/deepseek-v3.2"
summarizer = "deepseek/deepseek-v3.2"

[settings]
context_messages = 5
summarize_threshold = 30
knowledge_max_facts = 50
max_replan_depth = 3
max_validation_retries = 3
exec_timeout = 120
worker_idle_timeout = 300
host = "0.0.0.0"
port = 8333
# webhook_allow_list = ["127.0.0.1", "::1"]
```

### Required Fields

| Field | Description |
|---|---|
| `[tokens]` | At least one named token. Each client (CLI, connector) uses its own token. |
| `[providers]` | At least one provider with `base_url`. |
| `providers.*.api_key_env` | Env variable name for the API key. Optional for local providers (e.g. Ollama). |
| `providers.*.base_url` | Required. No implicit default. |
| `[users]` | At least one user. Each user has a `role` (`admin` or `user`). |
| `users.*.role` | Required. `"admin"` or `"user"`. |
| `users.*.skills` | Required for `user` role. `"*"` for all skills, or a list of skill names. Ignored for admins (always all). If missing on a `user` role, kiso refuses to start. |

### Optional Fields (defaults)

| Field | Default | Description |
|---|---|---|
| `users.*.aliases.*` | (none) | Platform identity per connector. Key = connector/token name, value = platform username. See [security.md](security.md). |
| `models.planner` | `moonshotai/kimi-k2.5` | Requires structured output (`response_format` with `json_schema`). |
| `models.reviewer` | `moonshotai/kimi-k2.5` | Requires structured output. |
| `models.curator` | `moonshotai/kimi-k2.5` | Requires structured output. |
| `models.worker` | `deepseek/deepseek-v3.2` | Free-form text, no constraints. |
| `models.summarizer` | `deepseek/deepseek-v3.2` | Free-form text, also used by paraphraser. |
| `settings.context_messages` | `5` | Number of recent raw messages sent to the planner (see [llm-roles.md](llm-roles.md#planner)). |
| `settings.summarize_threshold` | `30` | Summarizer triggers when raw message count reaches this value |
| `settings.knowledge_max_facts` | `50` | Max global facts before consolidation |
| `settings.max_replan_depth` | `3` | Max replan cycles per original message |
| `settings.max_validation_retries` | `3` | Max retries when planner returns structurally valid JSON that fails semantic validation |
| `settings.exec_timeout` | `120` | Seconds before exec or skill subprocess is killed |
| `settings.worker_idle_timeout` | `300` | Seconds before idle worker shuts down |
| `settings.host` | `0.0.0.0` | Server bind address |
| `settings.port` | `8333` | Server port |
| `settings.webhook_allow_list` | `[]` | IPs exempt from webhook SSRF validation (e.g. `["127.0.0.1"]` for local connectors). See [security.md — Webhook Validation](security.md#7-webhook-validation). |

## Tokens

Each client (CLI, connector, external service) gets its own named token. Revoking a client = removing its token and restarting.

```toml
[tokens]
cli = "tok-abc123"
discord = "tok-def456"
telegram = "tok-ghi789"
```

The token name identifies the connector for alias resolution and is logged on each call. Generate tokens however you want (`openssl rand -hex 32`, `uuidgen`, etc.). See [security.md — API Authentication](security.md#2-api-authentication) for details.

## Providers

All providers are OpenAI-compatible HTTP endpoints. Adding a provider = adding a section to config.

```toml
[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"

[providers.ollama]
base_url = "http://localhost:11434/v1"
```

- `api_key_env`: env var name for the API key. Read from env at startup — **never** stored in config. Optional for local providers (e.g. Ollama).
- `base_url`: **required**. No implicit default.

**Structured output requirement**: Planner, Reviewer, and Curator require `response_format` with strict `json_schema`. If the provider doesn't support it, the call fails with a clear error — no fallback. Worker, Summarizer, and Paraphraser produce free-form text and work with any provider.

## Model Routing

Model strings use `:` to specify a non-default provider. No `:` means the first listed provider.

```
moonshotai/kimi-k2.5             → first provider, model "moonshotai/kimi-k2.5"
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
