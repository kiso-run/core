# Preset Ecosystem & Installer Integration

> External devplan — presets, skills, tools, and installer UX improvements.
> These are outside the main kiso-run/core development cycle.

---

## Phase A — Installer & Preset Infrastructure

### M718 — Installer post-install preset step

**Problem:** After `install.sh` finishes and the container is healthy, the user has
a blank instance with zero tools, skills, or knowledge. They must manually discover
and install everything. Adding a "pick a preset" step right after the healthcheck
would dramatically improve first-run experience.

**Files:** `install.sh`

**Change:**

- [ ] After the healthcheck succeeds (line ~1003), add an optional interactive block:
      1. Fetch preset list from registry (`curl -sf .../registry.json | python3 -c ...`)
      2. Display numbered menu: "0) Skip — I'll set things up myself", then one line
         per preset (name + description)
      3. On selection, run `docker exec $CONTAINER kiso preset install <name>`
      4. Non-interactive mode (`--preset <name>` flag or `[[ ! -t 0 ]]`): skip menu,
         apply if flag given, skip otherwise
- [ ] Add `--preset <name>` CLI argument to install.sh arg parser
- [ ] In update-instance mode: skip preset step (presets are additive, not idempotent
      on re-run — user can do `kiso preset install` manually)
- [ ] Test: source install.sh with `KISO_INSTALL_LIB=1`, verify new functions exist

---

### M723 — `preset install` auto-installs tools

**Problem:** Currently `preset install` only *prints* tool/skill install instructions
but doesn't actually install them. The user must copy-paste each command manually.
For presets to work as one-step setup, install should orchestrate tool installation.

**Files:** `cli/preset_ops.py`, `cli/preset.py`

**Change:**

- [ ] In `install_preset()`, after seeding knowledge, iterate `manifest.tools` and
      call the tool install logic programmatically (reuse `_plugin_install` from
      `plugin_ops.py`) for each tool
- [ ] Same for `manifest.connectors` (if any)
- [ ] Skills (MD) are bundled in the preset dir — copy them to `~/.kiso/skills/`
- [ ] On failure of any single tool install: warn but continue (don't abort the
      whole preset)
- [ ] Add `--no-tools` flag to skip automatic tool installation
- [ ] Track which tools were actually installed in the tracking JSON
- [ ] Update `remove_preset` to optionally remove installed tools (`--with-tools` flag)
- [ ] Tests: mock `_plugin_install`, verify it's called for each tool in manifest

---

### M730 — Preset validation in CI

**Problem:** As presets grow, we need to ensure they stay valid. A CI step
should validate all `presets/*/preset.toml` files on every push.

**Files:** `tests/test_presets.py` (extend existing)

**Change:**

- [ ] Add a parametrized test that discovers all `presets/*/preset.toml` files
      and runs `load_preset()` on each — fails on validation errors
- [ ] Verify each preset's tools exist in `registry.json`
- [ ] Verify behaviors are non-empty strings >= 20 chars (no placeholder junk)
- [ ] Add to CI pipeline (should already be covered by `pytest tests/`)

---

## Phase B — Preset Bundles

### M719 — Create `basic` preset — starter kit

**Problem:** No concrete preset.toml files exist yet. The "basic" preset is the
universal starter: gives kiso web search, browsing, and code editing capabilities.

**Files:** `presets/basic/preset.toml` (new)

**Manifest:**

```toml
[kiso]
type = "preset"
name = "basic"
version = "1.0.0"
description = "Starter kit — web search, browser, and code editing"

[kiso.preset]
tools = ["websearch", "aider", "browser"]
skills = []
connectors = []

[kiso.preset.knowledge]
facts = []

behaviors = [
  "When the user asks a factual question, search the web before answering — do not guess.",
  "When editing code, always show a diff summary before and after the change.",
  "When browsing a URL, take a screenshot after navigation to confirm the page loaded.",
]

[kiso.preset.env]
KISO_TOOL_WEBSEARCH_API_KEY = { required = true, description = "Brave or Serper API key for web search" }
```

**Change:**

- [ ] Create `presets/basic/preset.toml` with the manifest above
- [ ] Add `basic` to `registry.json` presets array (update description)
- [ ] Unit test: `load_preset("presets/basic/preset.toml")` validates without errors
- [ ] Unit test: manifest has exactly 3 tools, 3 behaviors, 1 required env var

---

### M720 — Create `developer` preset

**Problem:** Developers need code editing, search, browsing, plus guidance on
TDD, git conventions, and structured code review.

**Files:** `presets/developer/preset.toml` (new)

**Manifest:**

```toml
[kiso]
type = "preset"
name = "developer"
version = "1.0.0"
description = "Software development — code editing, TDD, code review, git workflow"

[kiso.preset]
tools = ["aider", "websearch", "browser"]
skills = []
connectors = []

[kiso.preset.knowledge]
facts = []

behaviors = [
  "Follow TDD: write a failing test first, then implement, then refactor.",
  "When reviewing code, check: correctness, security (OWASP top 10), performance, readability — in that order.",
  "Commit messages: imperative mood, max 72 chars subject, body explains why not what.",
  "Before modifying a function, read its callers to understand the impact.",
  "When debugging, reproduce the bug first with a minimal test case before proposing a fix.",
  "Never push to main/master directly — always work on a branch.",
]

[kiso.preset.env]
KISO_TOOL_WEBSEARCH_API_KEY = { required = true, description = "Brave or Serper API key for web search" }
```

**Change:**

- [ ] Create `presets/developer/preset.toml`
- [ ] Add/update `developer` entry in `registry.json` (replace `backend-developer`)
- [ ] Unit test: validates, has correct tools and behavior count

---

### M721 — Create `researcher` preset

**Problem:** Research-oriented users need deep search + browsing + structured
methodology for source verification and synthesis.

**Files:** `presets/researcher/preset.toml` (new)

**Manifest:**

```toml
[kiso]
type = "preset"
name = "researcher"
version = "1.0.0"
description = "Deep research — web search, source verification, structured synthesis"

[kiso.preset]
tools = ["websearch", "browser"]
skills = []
connectors = []

[kiso.preset.knowledge]
facts = []

behaviors = [
  "Always cite sources with URLs when presenting research findings.",
  "Cross-verify claims from at least 2 independent sources before stating them as fact.",
  "When summarizing research, structure output as: key findings, supporting evidence, limitations/caveats.",
  "Prefer primary sources (official docs, papers, data) over secondary (blog posts, forums).",
  "When a source is behind a paywall or inaccessible, say so explicitly — never fabricate content.",
]

[kiso.preset.env]
KISO_TOOL_WEBSEARCH_API_KEY = { required = true, description = "Brave or Serper API key for web search" }
```

**Change:**

- [ ] Create `presets/researcher/preset.toml`
- [ ] Add to `registry.json`
- [ ] Unit test: validates, has 2 tools and 5 behaviors

---

### M722 — Create `assistant` preset

**Problem:** Personal assistant use case: email, calendar, docs via Google
Workspace, plus search and browsing for everyday tasks.

**Files:** `presets/assistant/preset.toml` (new)

**Manifest:**

```toml
[kiso]
type = "preset"
name = "assistant"
version = "1.0.0"
description = "Personal assistant — email, calendar, documents, web search"

[kiso.preset]
tools = ["gworkspace", "websearch", "browser"]
skills = []
connectors = []

[kiso.preset.knowledge]
facts = []

behaviors = [
  "When composing emails, always show a draft and ask for confirmation before sending.",
  "For calendar events, confirm date/time/timezone with the user before creating.",
  "When asked about schedules, check the calendar first — do not assume availability.",
  "Keep responses concise and action-oriented for productivity tasks.",
]

[kiso.preset.env]
KISO_TOOL_WEBSEARCH_API_KEY = { required = true, description = "Brave or Serper API key for web search" }
KISO_TOOL_GWORKSPACE_SERVICE_ACCOUNT = { required = true, description = "Google service account JSON for Workspace access" }
```

**Change:**

- [ ] Create `presets/assistant/preset.toml`
- [ ] Add to `registry.json`
- [ ] Unit test: validates, has gworkspace in tools

---

## Phase C — MD Skills

### M724 — MD skill: `code-review`

**Files:** `skills/code-review.md` (new, ships with repo)

**Change:**

- [ ] Create `skills/code-review.md` (security -> correctness -> performance -> readability)
- [ ] Add `code-review` to skills section in `registry.json`
- [ ] Unit test: `discover_md_skills()` finds it, frontmatter parses correctly

---

### M725 — MD skill: `writing`

**Files:** `skills/writing.md` (new)

**Change:**

- [ ] Create `skills/writing.md` (structure, audience, SEO, editing pass)
- [ ] Add to `registry.json`
- [ ] Unit test: frontmatter validates

---

### M726 — MD skill: `data-analysis`

**Files:** `skills/data-analysis.md` (new)

**Change:**

- [ ] Create `skills/data-analysis.md` (explore first, viz, stats, reproducibility)
- [ ] Add to `registry.json`
- [ ] Unit test: frontmatter validates

---

## Phase D — Tool Ideas (design docs only)

### M727 — Tool idea: `github`

**Problem:** Developer workflows frequently involve GitHub: reading issues,
creating PRs, reviewing diffs, checking CI status. Currently kiso can only
`exec curl` against the GitHub API, which is verbose and error-prone.

**Files:** `docs/tool-ideas/github.md` (new — design doc, not implementation)

**Scope:**

```
Actions:
  - issue_list, issue_get, issue_create, issue_comment
  - pr_list, pr_get, pr_create, pr_review
  - repo_search, actions_status

Env: KISO_TOOL_GITHUB_TOKEN (PAT or GitHub App token)
Deps: gh CLI (optional, fallback to REST API via httpx)
```

**Change:**

- [ ] Write design doc with args schema, examples, security considerations
- [ ] Add `github` placeholder to `registry.json`

---

### M728 — Tool idea: `imggen`

**Problem:** Image generation is a high-demand capability. Thin wrapper around
DALL-E / Flux APIs.

**Files:** `docs/tool-ideas/imggen.md` (new — design doc)

**Change:**

- [ ] Write design doc (generate action, backends, env, output to workspace)

---

### M729 — Tool idea: `document-reader`

**Problem:** Users need to process PDFs, DOCX, XLSX. No tool for this.

**Files:** `docs/tool-ideas/document-reader.md` (new — design doc)

**Change:**

- [ ] Write design doc (read/summarize/search actions, supported formats)
