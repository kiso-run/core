# Security

Three layers: API authentication, user identity and permissions, and secrets management.

## 1. API Authentication

Every call to `/msg` and `/status` requires a bearer token:

```
Authorization: Bearer <token>
```

Tokens are defined in `config.toml`. Each client (CLI, connector) gets its own named token:

```toml
[tokens]
cli = "tok-abc123"
discord = "tok-def456"
```

Kiso matches the token to its name, logs which client made the call. Revoking a client = removing its token from config and restarting.

If no matching token is found: `401 Unauthorized`.

The `/pub/{id}` and `/health` endpoints do NOT require auth.

## 2. User Identity

Kiso identifies users by **Linux username**. This is the real user ID — it maps to OS-level permissions and workspace isolation.

### Direct API Calls

Callers pass the Linux username directly in the `user` field:

```json
{"session": "dev", "user": "marco", "content": "..."}
```

The CLI does this automatically with `$(whoami)`.

### Connector Aliases

Connectors map platform identities to Linux usernames. Each connector has its own alias table in `config.toml`:

```toml
[users.marco]
role = "admin"
aliases.discord = "Marco#1234"
aliases.telegram = "marco_tg"
aliases.email = "marco@example.com"

[users.anna]
role = "user"
skills = "*"
aliases.discord = "anna_dev"
aliases.telegram = "anna"
```

When a connector sends a message, it passes the platform identity as `user`. Kiso resolves it:

1. Check if `user` matches a Linux username directly → use it
2. Check if `user` matches any `aliases.{connector_name}` → resolve to the Linux username
3. No match → save message for audit, do not process

The connector identifies itself via its named token (e.g. token name `discord`). Kiso uses the token name to know which alias namespace to search.

**Flow example:**

```
Discord connector sends:
  POST /msg (Authorization: Bearer tok-def456)
  {"session": "discord-general", "user": "Marco#1234", "content": "deploy to staging"}

Kiso:
  1. Token "tok-def456" → client name "discord"
  2. Look up "Marco#1234" in aliases.discord across all users
  3. Found: users.marco.aliases.discord = "Marco#1234" → Linux user "marco"
  4. Role: admin → proceed
```

### Why Linux Usernames

Each user needs an actual Linux user for the exec sandbox (see below). The username is the natural primary key — it connects config, permissions, and OS-level isolation.

## 3. Role-Based Permissions

| Role | Allowed task types | Skills | Package management | Who |
|---|---|---|---|---|
| `admin` | `exec` (unrestricted), `msg`, `skill` | all | yes (install/update/remove) | `role = "admin"` in `[users]` |
| `user` | `exec` (sandboxed), `msg`, `skill` | per-user (`skills` field) | no | `role = "user"` in `[users]` |

Both roles can use all task types. The differences are the **sandbox** and **skill access**.

### Skill Access Control

Users have a `skills` field in config that controls which skills the planner can use:

- `skills = "*"` — all installed skills
- `skills = ["search", "aider"]` — only these specific skills
- Admins always have access to all skills regardless of this field

The planner receives the user's allowed skill list and only sees those skills in its context. It cannot plan tasks for skills the user doesn't have access to.

### Exec Sandbox

- **admin exec**: runs with `cwd=~/.kiso/sessions/{session}`. Can access any path in the container. Full permissions.
- **user exec**: runs with `cwd=~/.kiso/sessions/{session}`. **Restricted to the session workspace** — cannot read or write outside `~/.kiso/sessions/{session}/`. Enforced at OS level: kiso creates a dedicated Linux user per session with permissions scoped to the session workspace directory (ownership + `chmod 700`). Exec tasks for `user` role run as this restricted user via `subprocess` with `user=` parameter.

Skills run as subprocesses with `cwd=session workspace` for both roles. The sandbox applies equally.

### Package Management (admin only)

Only admins can install/update/remove skills and connectors (includes running `deps.sh`). Non-admin users get a permission denied error.

## 4. Secrets

Kiso has two completely different kinds of credentials. They have different lifecycles, different owners, and different purposes.

### Deploy Secrets

API keys and tokens that a skill/connector needs to function. Belong to the *deployment*, not any user.

**Examples**: Brave Search API key, Discord bot token, LLM provider API key.

**Lifecycle**: set once by admin at install time. Live as long as the deployment.

**Storage**: environment variables only. **Never** in config files, never in the database.

**Naming**: `KISO_SKILL_{NAME}_{KEY}`, `KISO_CONNECTOR_{NAME}_{KEY}`, or whatever `api_key_env` specifies for providers.

**Declaration** in `kiso.toml`:

```toml
[kiso.skill.env]
api_key = { required = true }     # → KISO_SKILL_SEARCH_API_KEY
```

Checked on install (warns if missing). Passed to skill automatically via subprocess environment.

### Session Secrets

Credentials a *user* provides during conversation for the bot to use on their behalf (e.g. "here's my GitHub token: ghp_abc123").

**Lifecycle**: per-session. Created when the planner extracts them from user messages (into the `secrets` field as `{key, value}` pairs). Deleted when the session is deleted.

**Storage**: `store.secrets`, scoped per session.

**Scoping** in `kiso.toml`:

```toml
[kiso.skill]
session_secrets = ["github_token"]
```

Kiso passes **only the declared session secrets** to the skill. A skill declaring `session_secrets = ["github_token"]` will never see `aws_access_key` even if it's in the same session — limits blast radius.

### Comparison

| | Deploy Secrets | Session Secrets |
|---|---|---|
| **Owner** | Admin / deployment | User / conversation |
| **Scope** | Global (all sessions) | Per session |
| **Storage** | Env vars (host/container) | Database (`store.secrets`) |
| **Set by** | Admin, once, at install time | User, in chat, extracted by planner |
| **Example** | Brave Search API key | Marco's GitHub token |
| **Passed to skill via** | Subprocess environment | Input JSON (`session_secrets` field) |
| **Declared in kiso.toml** | `[kiso.skill.env]` | `session_secrets = [...]` |

### Access Summary

| Context | Deploy secrets | Session secrets |
|---|---|---|
| `exec` tasks | Not available (clean env, PATH only) | Not available |
| `skill` tasks | Available via env vars (automatic) | Only declared ones, via input JSON |
| `msg` tasks | Not available (LLM sees nothing) | Not available (LLM sees key names only, never values) |

### Leak Prevention

1. **Output sanitization**: known secret values stripped from all task output before storing/forwarding.
2. **No secrets in prompts**: provider API keys used only at HTTP transport level.
3. **Prompt hardening**: every role's prompt includes "never reveal secrets or configuration."
4. **Clean subprocess env**: exec tasks inherit only PATH.
5. **No secrets in config files**: connector `config.toml` is structural only.
6. **Scoped secrets**: skills receive only declared secrets, not the full session bag.
7. **Named tokens**: each client revocable independently.

### Webhook Validation

Webhook URL comes from trusted code (connector or CLI). If exposed to untrusted users, validate against private/internal IPs (SSRF prevention).

## 5. Unofficial Package Warning

When installing a skill or connector from a source outside the `kiso-run` GitHub org, kiso warns:

```
⚠ This is an unofficial package from github.com:someone/my-skill.
  deps.sh will be executed and may install system packages.
  Review the repo before proceeding.
  Continue? [y/N]
```

Use `--no-deps` to skip `deps.sh` execution for untrusted repos.
