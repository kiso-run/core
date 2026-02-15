# Configuration

## What Goes Where

**JSON** for static stuff you edit by hand: providers, models, settings.

**SQLite** for everything dynamic: sessions, messages, secrets, facts, published files.

```
~/.kiso/
├── config.json          # static, human-readable, versionable
└── store.db             # dynamic, machine-managed
```

## Minimal config.json

An empty `config.json` (or just `{}`) is enough to start — all fields have sensible defaults. Kiso will auto-generate an API token on first boot and use the Linux user system for identity.

**API keys are never stored in config.json.** They are read from environment variables so that the config can be safely versioned and backed up. The config only specifies *which* env variable to use (see Providers below).

## Full config.json

All fields with their defaults:

```json
{
  "api_token": "auto-generated-on-first-boot",
  "admins": [],

  "providers": {
    "openrouter": {
      "api_key_env": "KISO_OPENROUTER_API_KEY",
      "base_url": "https://openrouter.ai/api/v1"
    }
  },

  "models": {
    "planner":    "moonshotai/kimi-k2.5",
    "reviewer":   "moonshotai/kimi-k2.5",
    "worker":     "deepseek/deepseek-v3.2",
    "summarizer": "deepseek/deepseek-v3.2"
  },

  "summarize_threshold": 30,
  "knowledge_max_lines": 50,
  "max_review_depth": 3,
  "max_replan_depth": 3,
  "exec_timeout": 120,
  "worker_idle_timeout": 300,
  "host": "0.0.0.0",
  "port": 8333
}
```

### Defaults

| Field | Default | Description |
|---|---|---|
| `api_token` | auto-generated | Bearer token for API auth. Generated on first boot, printed to stdout. |
| `admins` | `[]` | Linux usernames with admin role. Empty = nobody is admin. |
| `providers` | openrouter | At least one required. Each provider reads its API key from an env variable (`api_key_env`). |
| `models.planner` | `moonshotai/kimi-k2.5` | Strong reasoning model for planning and deciding |
| `models.reviewer` | `moonshotai/kimi-k2.5` | Strong reasoning model for evaluating task output |
| `models.worker` | `deepseek/deepseek-v3.2` | Fast model for text generation and conversation |
| `models.summarizer` | `deepseek/deepseek-v3.2` | Fast model for compression |
| `summarize_threshold` | `30` | Raw messages before summarizer triggers |
| `knowledge_max_lines` | `50` | Max lines of facts before consolidation |
| `max_review_depth` | `3` | Max reviewer inject rounds per chain |
| `max_replan_depth` | `3` | Max replan cycles per original message. After this, the worker notifies the user of failure and moves on. |
| `exec_timeout` | `120` | Seconds before exec task is killed |
| `worker_idle_timeout` | `300` | Seconds before idle worker shuts down |
| `host` | `0.0.0.0` | Server bind address |
| `port` | `8333` | Server port |

## Providers

All providers are OpenAI-compatible HTTP endpoints. Adding a provider = adding a few lines to config.json.

```json
{
  "providers": {
    "openrouter": {
      "api_key_env": "KISO_OPENROUTER_API_KEY",
      "base_url": "https://openrouter.ai/api/v1"
    },
    "ollama": {
      "base_url": "http://localhost:11434/v1"
    }
  }
}
```

- `api_key_env`: name of the environment variable containing the API key. Kiso reads the key from the env at startup — it is **never** stored in config.json.
- `base_url`: **required**. No implicit default — if missing, Kiso refuses to start. This follows the "no magic" principle: every provider must be explicitly configured.

## Model Routing

Model strings use `:` to specify a non-default provider. No `:` means openrouter.

```
moonshotai/kimi-k2               → openrouter, model "moonshotai/kimi-k2"
deepseek/deepseek-r1             → openrouter, model "deepseek/deepseek-r1"
ollama:llama3                    → provider "ollama", model "llama3"
```

`llm.py` splits on the first `:`, looks up the provider in config, and makes the call with the right `base_url` and `api_key`.

## User Identity

Kiso uses Linux usernames as user identifiers:

- **CLI**: `user` defaults to `$(whoami)`
- **Connectors**: map platform users to Linux usernames in their own config
- **admins list**: contains Linux usernames

See [security.md](security.md).
