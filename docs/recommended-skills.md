# Recommended skills (outside the default)

Skills that are useful but not shipped in the default install.
Populated by M1515 (trust model + recommended-skills docs) —
kept here as a placeholder so M1503's cross-links resolve.

## Format (to be finalised in M1515)

Each entry will have:

- **Source** — GitHub URL or `agentskills.io` slug
- **Purpose** — one-line description
- **Trust tier** — Tier 1 (Anthropic / vetted), Tier 2
  (`kiso-run`), or community
- **Install hint** — `kiso skill install --from-url <url>`

## Placeholder entries

These exist as structure hints; real entries arrive with M1515.

### `devops-runbooks` *(example structure)*

- **Source**: *(to be curated)*
- **Purpose**: deploy / rollback / incident-response runbooks
  projected into planner + worker role sections
- **Trust tier**: TBD
- **Install hint**: TBD

### `writing-style-kit` *(example structure)*

- **Source**: *(to be curated)*
- **Purpose**: tone guides + review heuristics for the messenger
- **Trust tier**: TBD
- **Install hint**: TBD

---

## Discovery

- [agentskills.io](https://agentskills.io) — the standard's
  public registry
- [GitHub topic: `agent-skills`](https://github.com/topics/agent-skills)
- Anthropic's curated skill collection (link to follow when
  M1515 lands)

**Safety reminder**: skills can declare `scripts/` and
`allowed-tools` that expand what the agent is willing to
execute. `kiso skill install --from-url` warns on these; review
before approving.
