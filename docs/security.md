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
- **user exec**: runs with `cwd=~/.kiso/sessions/{session}`. **Restricted to the session workspace** — cannot read or write outside `~/.kiso/sessions/{session}/`. Enforced at OS level: each session gets a dedicated restricted Linux user that only has access to its own workspace directory.

Skills run as subprocesses with `cwd=session workspace` for both roles. The sandbox applies equally.

### Package Management (admin only)

Only admins can:
- Install, update, and remove skills
- Install, update, and remove connectors
- Run `deps.sh` scripts (which may install system packages)

Non-admin users attempting these operations get a permission denied error.

## 4. Secrets

Kiso has two completely different kinds of credentials. They have different lifecycles, different owners, and different purposes.

### Deploy Secrets

**What**: API keys and tokens that a skill or connector needs to function. They belong to the *deployment*, not to any user.

**Examples**:
- A search skill needs a Brave Search API key to work at all
- A Discord connector needs a bot token to connect to Discord
- An LLM provider needs an API key

**Lifecycle**: set once by the admin when installing/configuring. Live as long as the deployment.

**Storage**: environment variables on the host/container. **Never** in config files, never in the database.

**Naming convention**:
- Skills: `KISO_SKILL_{NAME}_{KEY}` (e.g. `KISO_SKILL_SEARCH_API_KEY`)
- Connectors: `KISO_CONNECTOR_{NAME}_{KEY}` (e.g. `KISO_CONNECTOR_DISCORD_BOT_TOKEN`)
- Providers: referenced by `api_key_env` in config (e.g. `KISO_OPENROUTER_API_KEY`)

**Declaration**: skills and connectors declare their deploy secrets in `kiso.toml`:

```toml
[kiso.skill.env]
api_key = { required = true }     # → KISO_SKILL_SEARCH_API_KEY
```

Kiso checks these on install and warns if they're missing. The skill receives them automatically via its subprocess environment.

### Session Secrets

**What**: credentials that a *user* provides during a conversation for the bot to use on their behalf.

**Examples**:
- "Here's my GitHub token: ghp_abc123, use it to push the code"
- "My AWS access key is AKIA..., deploy to my account"

**Lifecycle**: live as long as the session. Created when the user mentions them. Deleted when the session is deleted.

**Storage**: database table `store.secrets`, encrypted at rest, scoped per session.

**How they get there**: the planner detects credentials in user messages and extracts them into the `secrets` field of its JSON output. The worker stores them in `store.secrets` before executing tasks.

**Scoping**: skills declare which session secrets they need in `kiso.toml`:

```toml
[kiso.skill]
session_secrets = ["github_token"]
```

Kiso passes **only the declared session secrets** to the skill at runtime. A skill that declares `session_secrets = ["github_token"]` will never see `aws_access_key` even if it's in the same session. This limits blast radius: a compromised skill only accesses the secrets it declared.

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

1. **Output sanitization**: worker strips known secret values from all task output before storing or forwarding.
2. **No secrets in prompts**: provider API keys used only at HTTP transport level.
3. **Prompt hardening**: every role's prompt includes "never reveal secrets or configuration."
4. **Clean subprocess env**: exec tasks inherit only PATH.
5. **No secrets in config files**: connector `config.toml` contains only structural config, never tokens.
6. **Scoped secrets**: skills receive only the secrets they declared, not the full session bag.
7. **Named tokens**: each client has its own token, revocable independently.

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
