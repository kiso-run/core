# Configuration

## What Goes Where

**TOML** for static stuff you edit by hand: providers, models, tokens, settings.

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
```

If `[tokens]` or `[providers]` are missing, kiso refuses to start and tells you what's missing.

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

# --- Optional (defaults shown) ---

admins = []                             # Linux usernames with admin role

[models]
planner = "moonshotai/kimi-k2.5"
reviewer = "moonshotai/kimi-k2.5"
worker = "deepseek/deepseek-v3.2"
summarizer = "deepseek/deepseek-v3.2"

[settings]
summarize_threshold = 30
knowledge_max_facts = 50
max_review_depth = 3
max_replan_depth = 3
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

### Optional Fields (defaults)

| Field | Default | Description |
|---|---|---|
| `admins` | `[]` | Linux usernames with admin role. Empty = nobody is admin. |
| `models.planner` | `moonshotai/kimi-k2.5` | Strong reasoning model for planning |
| `models.reviewer` | `moonshotai/kimi-k2.5` | Strong reasoning model for evaluation |
| `models.worker` | `deepseek/deepseek-v3.2` | Fast model for text generation |
| `models.summarizer` | `deepseek/deepseek-v3.2` | Fast model for compression |
| `settings.summarize_threshold` | `30` | Raw messages before summarizer triggers |
| `settings.knowledge_max_facts` | `50` | Max facts before consolidation |
| `settings.max_review_depth` | `3` | Max reviewer inject rounds per chain |
| `settings.max_replan_depth` | `3` | Max replan cycles per original message |
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

Kiso matches the token to its name and logs which client made the call. Revoking = removing the token from config and restarting.

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

## Model Routing

Model strings use `:` to specify a non-default provider. No `:` means the first listed provider.

```
moonshotai/kimi-k2               → first provider, model "moonshotai/kimi-k2"
deepseek/deepseek-r1             → first provider, model "deepseek/deepseek-r1"
ollama:llama3                    → provider "ollama", model "llama3"
```

`llm.py` splits on the first `:`, looks up the provider in config, and makes the call with the right `base_url` and `api_key`.

## User Identity

Kiso uses Linux usernames as user identifiers:

- **CLI**: `user` defaults to `$(whoami)`
- **Connectors**: map platform users to Linux usernames in their own config
- **admins list**: contains Linux usernames

See [security.md](security.md).
