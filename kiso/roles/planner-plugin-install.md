Plugin installation: when the user asks for a capability and no matching skill/connector is installed:

1. **Named request** ("install skill X", "installa il connector Y") → go to step 3.
2. **Ambiguous request** (user asks for a capability, not a specific plugin): exec `curl <registry_url>` (see "Plugin registry" in System Environment) to discover matching plugins. NEVER use `kiso skill search` or web search for plugin discovery. Then replan to evaluate results.
3. **Read requirements**: exec `curl https://raw.githubusercontent.com/kiso-run/skill-{name}/main/kiso.toml` (or `connector-{name}`) to read env var requirements and descriptions.
4. **Check env vars**: if the kiso.toml shows required env vars:
   - If ALL required vars are already set (confirmed in prior outputs) → proceed to step 5.
   - If vars are MISSING → msg user asking for each value (include the description from kiso.toml). Then replan.
5. **Install** (can combine with step 3 in one plan when no env vars are needed):
   - exec `kiso env set KEY VALUE` for each required var (if user provided them).
   - exec `kiso skill install {name}` (or `kiso connector install {name}`).
   - exec `kiso connector run {name}` if it is a connector.
6. **Replan after install** to use the newly installed skill (the next planner call sees the fresh skill list).

Key principle: minimize replans. Steps 3+5 can be in ONE plan when no env vars are missing. The only mandatory replan points are:
- After registry discovery (step 2) when the result needs evaluation
- After asking user for env vars (step 4)
- After install (step 6) so the planner sees the new skill in its Skills section

When writing msg task details that refer to kiso commands, always use the exact syntax from the Kiso management commands list.
