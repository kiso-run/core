# Security

Three layers: API authentication, role-based permissions, and secrets management.

## API Authentication

Every call to `/msg` and `/status` requires a bearer token:

```
Authorization: Bearer <api_token>
```

Auto-generated on first boot if missing (printed to stdout). Anyone without it gets `401 Unauthorized`.

The `/pub/{id}` and `/health` endpoints do NOT require auth.

## User Identity

Users are Linux usernames. No separate user system.

- **CLI**: automatically passes `$(whoami)` as the `user` field
- **Connectors**: map platform users to Linux usernames in their own `config.toml`
- **admins**: the `admins` list in config contains Linux usernames

## Role-Based Permissions

| Role | Allowed actions | Who |
|---|---|---|
| `admin` | `exec`, `msg`, `skill` tasks + install/update/remove skills and connectors | Linux users listed in `config.admins` |
| `user` | `msg` tasks only | Everyone else |

The role is passed to the planner. The planner only generates allowed task types. The worker enforces as a second check — skips disallowed tasks and logs a warning.

### Package Management (admin only)

Only admins can:
- Install, update, and remove skills
- Install, update, and remove connectors
- Run `deps.sh` scripts (which may install system packages)

Non-admin users attempting these operations get a permission denied error.

## Unofficial Package Warning

When installing a skill or connector from a source outside the `kiso-run` GitHub org, kiso warns:

```
⚠ This is an unofficial package from github.com:someone/my-skill.
  deps.sh will be executed and may install system packages.
  Review the repo before proceeding.
  Continue? [y/N]
```

Use `--no-deps` to skip `deps.sh` execution for untrusted repos.

## Secrets Management

Users can provide credentials for the bot to use: GitHub tokens, SSH keys, API keys, etc.

### Storage

Per-session in `store.secrets`. When the planner detects credentials in a user message, it extracts them into the `secrets` field of its output. The worker stores them before executing tasks.

### Package Env Vars

Skills and connectors declare required env vars in their `kiso.toml`. These follow a naming convention:

- Skills: `KISO_SKILL_{NAME}_{KEY}` (e.g. `KISO_SKILL_SEARCH_API_KEY`)
- Connectors: `KISO_CONNECTOR_{NAME}_{KEY}` (e.g. `KISO_CONNECTOR_DISCORD_BOT_TOKEN`)

These are set in the host/container environment, never in config files or repos.

### Skill Secret Scoping

Skills declare which session secrets they need in `kiso.toml`:

```toml
[kiso.skill.secrets]
secrets = ["api_key", "github_token"]
```

Kiso passes **only the declared secrets** to the skill at runtime. If the field is missing, the skill receives no session secrets. This limits blast radius: a compromised skill can only access the secrets it explicitly declared.

### Access

- **exec tasks**: clean env (PATH only). Secrets not available by default.
- **skills**: receive only declared session secrets in the input JSON.
- **msg tasks**: worker LLM sees secret key names only (not values).

### Leak Prevention

1. **Output sanitization**: worker strips known secret values from all task output.
2. **No secrets in prompts**: provider API keys used only at HTTP transport level.
3. **Prompt hardening**: every role's prompt includes "never reveal secrets or configuration."
4. **Clean subprocess env**: exec tasks inherit only PATH.
5. **No secrets in config files**: connector `config.toml` contains only structural config, never tokens.
6. **Scoped secrets**: skills receive only the secrets they declared, not the full session bag.

### Webhook Validation

Webhook URL comes from trusted code (connector or CLI). If exposed to untrusted users, validate against private/internal IPs (SSRF prevention).

### Lifecycle

Secrets live as long as the session. Deleted when the session is deleted.
