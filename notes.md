## Punti aperti

### 1. Access control per-skill
- L'admin controlla chi può installare. Ma serve anche un controllo su chi può *usare* una skill specifica?
- Opzione: lista `allowed_users` per skill in config.json
- Da decidere dopo l'uso reale

### 2. Connector restart policy
- kiso gestisce i connector come daemon. Che restart policy?
- Opzioni: always restart, max retries, backoff esponenziale
- Da definire

## Decisioni prese

### Package system (skill + connector)
- ✅ `kiso.toml` manifest per ogni pacchetto (type, name, version, env vars, deps)
- ✅ `pyproject.toml` + `uv` per dipendenze python — **required**, nessun fallback
- ✅ `deps.sh` idempotente per dipendenze di sistema
- ✅ SKILL.md separato per le skill (serve al worker, formato libero)
- ✅ Planner vede solo la one-liner da kiso.toml, non SKILL.md
- ✅ Env var convention: `KISO_{TYPE}_{NAME}_{KEY}`
- ✅ Secrets solo in env vars, mai in file
- ✅ Solo admin può installare/aggiornare/rimuovere
- ✅ Warning per repo non ufficiali + opzione `--no-deps`
- ✅ Skill secret scoping: dichiarano quali secrets ricevono
- ✅ Connector config: `config.toml` (no secrets) + `config.example.toml` nel repo

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
- ✅ Task persistiti in DB (non più in-memory) — sopravvivono ai restart
- ✅ `GET /health` per Docker healthcheck
