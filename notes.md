## Punti aperti

### 1. Access control per-skill
- L'admin controlla chi può installare. Ma serve anche un controllo su chi può *usare* una skill specifica?
- Opzione: lista `allowed_users` per skill in config.toml
- Da decidere dopo l'uso reale

### 2. Connector restart policy
- kiso gestisce i connector come daemon. Che restart policy?
- Opzioni: always restart, max retries, backoff esponenziale
- Da definire

### 3. User exec sandbox implementation
- User exec è sandboxed alla session workspace. Come enforciamo?
- Opzioni: Linux user ristretto per sessione, namespaces, seccomp
- Da definire nell'implementazione

## Decisioni prese

### Package system (skill + connector)
- ✅ `kiso.toml` manifest è la singola fonte di verità (type, name, version, args schema, env vars, deps, secrets)
- ✅ `pyproject.toml` + `uv` per dipendenze python — **required**, nessun fallback
- ✅ `deps.sh` idempotente per dipendenze di sistema
- ✅ SKILL.md eliminato come file required — lo schema args è in kiso.toml
- ✅ Planner vede one-liner + args schema da kiso.toml
- ✅ Env var convention: `KISO_{TYPE}_{NAME}_{KEY}`
- ✅ Secrets solo in env vars, mai in file
- ✅ Skill secret scoping: dichiarano quali secrets ricevono
- ✅ Solo admin può installare/aggiornare/rimuovere
- ✅ Warning per repo non ufficiali + opzione `--no-deps`
- ✅ Connector config: `config.toml` (no secrets) + `config.example.toml` nel repo

### Config
- ✅ Tutto TOML — `config.toml` per il core, `kiso.toml` per i pacchetti, `config.toml` per i connector
- ✅ No magic: tokens e providers richiesti esplicitamente, kiso rifiuta di partire se mancano
- ✅ Multi-token: ogni client (CLI, connector) ha il suo token nominato, revocabile
- ✅ Planner parse failure: errore esplicito, niente fallback silente
- ✅ `expect` required quando `review: true`

### Permessi
- ✅ User può fare exec (sandboxed a sessione), msg, skill
- ✅ Admin può fare exec (unrestricted), msg, skill, + package management
- ✅ Named tokens per client, revocabili singolarmente

### Database
- ✅ 7 tabelle: sessions, messages, tasks, facts, secrets, meta, published
- ✅ Tasks persistiti in DB, non in-memory
- ✅ Facts come tabella dedicata (non blob in meta) — entries individuali, consolidati dal summarizer

### Repo e discovery
- ✅ Core: `git@github.com:kiso-run/core.git`
- ✅ Skills: `git@github.com:kiso-run/skill-{name}.git` (topic: `kiso-skill`)
- ✅ Connectors: `git@github.com:kiso-run/connector-{name}.git` (topic: `kiso-connector`)
- ✅ Search via GitHub API: `org:kiso-run+topic:kiso-skill`
- ✅ Non ufficiali: URL git diretto, nome auto-generato `{domain}_{namespace}_{repo}`

### CLI
- ✅ Noun-verb: `kiso skill search/install/update/remove/list`
- ✅ `kiso connector <name> run/stop/status` — daemon gestiti da kiso

### Infra
- ✅ Docker come ambiente di default
- ✅ Volume singolo `~/.kiso/` per tutti i dati
- ✅ Skill/connector installabili nel Dockerfile (build-time) o nel volume (runtime)
- ✅ `GET /health` per Docker healthcheck
