# Configuration

## What Goes Where

**TOML** for static stuff you edit by hand: providers, models, tokens, users, settings.

**SQLite** for everything dynamic: sessions, messages, tasks, facts, secrets, published files.

```
~/.kiso/
├── config.toml          # static, human-readable, versionable
└── store.db             # dynamic, machine-managed
```

## Minimal config.toml

Kiso requires explicit configuration for anything that talks to external services. No magic, no guessing.

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
# --- Required ---

[tokens]
cli = "your-secret-token"              # used by the CLI
# discord = "another-token"            # used by the discord connector
# telegram = "yet-another-token"       # each client gets its own token

[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"

# [providers.ollama]
# base_url = "http://localhost:11434/v1"

# --- Required ---

[users.marco]
role = "admin"                          # full access, package management
aliases.discord = "Marco#1234"          # platform identity on Discord
aliases.telegram = "marco_tg"           # platform identity on Telegram
aliases.email = "marco@example.com"     # platform identity on email

[users.anna]
role = "user"                           # sandboxed exec, all skills
skills = "*"
aliases.discord = "anna_dev"

[users.luca]
role = "user"                           # sandboxed exec, specific skills only
skills = ["search", "aider"]

# --- Optional (defaults shown) ---

[models]
planner = "moonshotai/kimi-k2.5"
reviewer = "moonshotai/kimi-k2.5"
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
```

### Required Fields

| Field | Description |
|---|---|
| `[tokens]` | At least one named token. Each client (CLI, connector) uses its own token. |
| `[providers]` | At least one provider with `base_url`. Kiso refuses to start without it. |
| `providers.*.api_key_env` | Env variable name for the API key. Optional for local providers (e.g. Ollama). |
| `providers.*.base_url` | Required. No implicit default. |
| `[users]` | At least one user. Each user has a `role` (`admin` or `user`). |
| `users.*.role` | Required. `"admin"` or `"user"`. |
| `users.*.skills` | Required for `user` role. `"*"` for all skills, or a list of skill names. Ignored for admins (always all). |

### Optional Fields (defaults)

| Field | Default | Description |
|---|---|---|
| `users.*.aliases.*` | (none) | Platform identity per connector. Key = connector/token name, value = platform username. See [security.md](security.md). |
| `models.planner` | `moonshotai/kimi-k2.5` | Must support structured output (`response_format` with `json_schema`). |
| `models.reviewer` | `moonshotai/kimi-k2.5` | Must support structured output. |
| `models.worker` | `deepseek/deepseek-v3.2` | Free-form text output, no constraints. |
| `models.summarizer` | `deepseek/deepseek-v3.2` | Free-form text output, no constraints. |
| `settings.context_messages` | `5` | Number of recent raw messages sent to the planner. Msg outputs are sent separately. |
| `settings.summarize_threshold` | `30` | Raw messages before summarizer triggers |
| `settings.knowledge_max_facts` | `50` | Max global facts before consolidation |
| `settings.max_replan_depth` | `3` | Max replan cycles per original message |
| `settings.max_validation_retries` | `3` | Max retries when planner returns structurally valid JSON that fails semantic validation |
| `settings.exec_timeout` | `120` | Seconds before exec task is killed |
| `settings.worker_idle_timeout` | `300` | Seconds before idle worker shuts down |
| `settings.host` | `0.0.0.0` | Server bind address |
| `settings.port` | `8333` | Server port |

## Tokens

Each client (CLI, connector, external service) gets its own named token. This allows revoking a single client without affecting others.

```toml
[tokens]
cli = "tok-abc123"
discord = "tok-def456"
telegram = "tok-ghi789"
```

API calls use the standard bearer header:

```
Authorization: Bearer tok-abc123
```

Kiso matches the token to its name and logs which client made the call. The token name also identifies the connector for alias resolution (see [security.md](security.md)). Revoking = removing the token from config and restarting.

Generate tokens however you want (`openssl rand -hex 32`, `uuidgen`, etc.). Kiso does not auto-generate tokens.

## Providers

All providers are OpenAI-compatible HTTP endpoints. Adding a provider = adding a section to config.

```toml
[providers.openrouter]
api_key_env = "KISO_OPENROUTER_API_KEY"
base_url = "https://openrouter.ai/api/v1"

[providers.ollama]
base_url = "http://localhost:11434/v1"
```

- `api_key_env`: name of the environment variable containing the API key. Kiso reads the key from the env at startup — it is **never** stored in config. Optional for local providers.
- `base_url`: **required**. No implicit default. If missing, kiso refuses to start.

**Structured output requirement**: the Planner and Reviewer use `response_format` with strict `json_schema`. The provider used for these roles must support it. If it doesn't, the call fails with a clear error — no silent fallback. Worker and Summarizer produce free-form text and work with any provider.

## Model Routing

Model strings use `:` to specify a non-default provider. No `:` means the first listed provider.

```
moonshotai/kimi-k2               → first provider, model "moonshotai/kimi-k2"
deepseek/deepseek-r1             → first provider, model "deepseek/deepseek-r1"
ollama:llama3                    → provider "ollama", model "llama3"
```

`llm.py` splits on the first `:`, looks up the provider in config, and makes the call with the right `base_url` and `api_key`.

## Users

Kiso uses a whitelist. Only users listed in `[users]` get responses. Messages from unknown users are saved to `store.messages` but not processed — no worker is spawned, no response is sent.

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
- **`skills`**: which skills the planner can use for this user. `"*"` = all installed skills. A list = only those. Admins always have all skills regardless of this field.
- **`aliases.*`**: maps platform identities to this Linux user. Key is the connector/token name, value is the platform username. See [security.md](security.md).

User identifiers are Linux usernames:
- **CLI**: `user` defaults to `$(whoami)` — direct API call, no alias needed
- **Connectors**: pass the platform identity as `user`, kiso resolves it via aliases
