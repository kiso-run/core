## Punti aperti

### 1. Docker setup
- Definire il Dockerfile: python + uv + tools comuni
- Volume mount per ~/.kiso/ (persistenza)
- Come esporre le porte (kiso API + webhook connector)

### 2. Access control per-skill
- L'admin controlla chi può installare. Ma serve anche un controllo su chi può *usare* una skill specifica?
- Opzione: lista `allowed_users` per skill in config.json
- Da decidere dopo l'uso reale

### 3. Connector lifecycle
- `kiso connector discord run/stop` — come gestire il processo?
- Opzioni: subprocess gestito da kiso, oppure solo wrapper per systemd/supervisor
- Da decidere

## Decisioni prese

### Package system (skill + connector)
- ✅ `kiso.toml` manifest per ogni pacchetto (type, name, version, env vars, deps)
- ✅ `pyproject.toml` + `uv` per dipendenze python (no requirements.txt)
- ✅ `deps.sh` idempotente per dipendenze di sistema
- ✅ SKILL.md separato per le skill (serve al worker)
- ✅ Env var convention: `KISO_{TYPE}_{NAME}_{KEY}`
- ✅ Secrets solo in env vars, mai in file
- ✅ Solo admin può installare/aggiornare/rimuovere

### Repo e discovery
- ✅ Core: `git@github.com:kiso-run/core.git`
- ✅ Skills: `git@github.com:kiso-run/skill-{name}.git` (topic: `kiso-skill`)
- ✅ Connectors: `git@github.com:kiso-run/connector-{name}.git` (topic: `kiso-connector`)
- ✅ Search via GitHub API: `org:kiso-run+topic:kiso-skill`
- ✅ Non ufficiali: URL git diretto, nome auto-generato `{domain}_{namespace}_{repo}`

### CLI
- ✅ Noun-verb: `kiso skill search/install/update/remove/list`
- ✅ `kiso connector <name> run/stop`
