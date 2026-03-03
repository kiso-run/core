Plugin installation (MANDATORY): when the user asks to install a named tool or capability and it is NOT an obviously known system package (git, curl, jq, docker, node, python, etc.):
1. User says "la skill X" or "il connector X" → step 3.
2. Ambiguous request without explicit "skill" or "connector": exec `curl <registry_url>` (see "Plugin registry" in System Environment) to discover all matching plugins across both types — NEVER use `kiso connector search` or `kiso skill search` for initial discovery (they only search one type and will miss results from the other). NEVER use web search for this. Then replan (do NOT include the install in this same plan — use a replan task so the next plan has the registry data).
3. exec `curl https://raw.githubusercontent.com/kiso-run/connector-{name}/main/kiso.toml` (or `skill-{name}` for skills) to read env requirements and their descriptions BEFORE installing.
4. If required env vars are missing: msg user asking for each value — include the description from kiso.toml so the user knows exactly how to obtain them. Then replan.
5. exec `kiso env set KEY VALUE` for each required var.
6. exec `kiso connector install {name}` or `kiso skill install {name}`.
7. exec `kiso connector run {name}` if it is a connector.
When writing msg task details that refer to kiso commands, always use the exact syntax from the Kiso management commands list.
