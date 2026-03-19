# Plugin Ecosystem — Development Plan

> Plugin-specific milestones, independent numbering (M1, M2, ...).
> Plugins live in `/home/ymx1zq/Documents/software/kiso-run/plugins/`.

---

## Plugin Compliance Audit

Reference: `docs/tools.md` (tool spec), `docs/connectors.md` (connector spec), `docs/tool-development.md` (devplan format).

### Status Matrix

| Plugin | kiso.toml | run.py | pyproject.toml | deps.sh | README.md | LICENSE | DEVPLAN.md | Env vars | Tests |
|--------|-----------|--------|----------------|---------|-----------|---------|------------|----------|-------|
| tool-browser | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| tool-websearch | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| tool-aider | ✅ | ⚠️ | ✅ | ❌ | ✅ | ✅ | ✅ | ❌ BUG | ✅ |
| tool-moltbook | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| tool-gworkspace | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| connector-discord | ✅ | ⚠️ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |

### Issues Found

1. **tool-aider — env var prefix BUG**: `run.py` reads `KISO_SKILL_AIDER_API_KEY` but the framework injects `KISO_TOOL_AIDER_API_KEY`. Also the comment in `kiso.toml` says the old prefix. Aider silently gets no API key at runtime. Live tests (`tests/live/test_roles.py`) also expect the old name.

2. **connector-discord — no file attachment handling**: When a Discord user sends a message with attachments (images, files), the connector ignores them. Per `docs/connectors.md`, connectors should download attachments and write them to `uploads/`. The planner can then decide what to do with them.

3. **Missing README.md**: tool-moltbook, tool-gworkspace.

4. **Missing LICENSE**: tool-moltbook, tool-gworkspace.

5. **Missing deps.sh**: tool-websearch, tool-aider, tool-moltbook, connector-discord. Not always needed (no system deps), but should exist as empty/no-op for consistency.

6. **tool-docreader**: Referenced in v0.7_wip.md M761 and in the registry, but the plugin doesn't exist yet.

---

## Phase 1 — Critical Bug Fixes

### M1 — tool-aider: fix KISO_SKILL_ → KISO_TOOL_ env var prefix ✅
- [x] In `tool-aider/run.py`, changed all `KISO_SKILL_AIDER_` → `KISO_TOOL_AIDER_`
- [x] Updated `kiso.toml` comment
- [x] Updated live tests in core (`tests/live/test_roles.py`)
- [x] Also fixed tool-moltbook: all `KISO_SKILL_MOLTBOOK_` → `KISO_TOOL_MOLTBOOK_` across run.py, kiso.toml, tests, DEVPLAN.md
- [x] Fixed core reference doc (`kiso/reference/skills.md`)
- [x] Existing installations need: `kiso env set KISO_TOOL_AIDER_API_KEY <value>`

### M2 — core: env var backward compat SKILL → TOOL ✅
- [x] In `build_tool_env` (kiso/tools.py), if `KISO_TOOL_{NAME}_{KEY}` is not set, fall back to `KISO_SKILL_{NAME}_{KEY}` in os.environ
- [x] Log a deprecation warning when the fallback is used
- [x] Unit tests: legacy fallback works, TOOL takes priority over SKILL, no fallback when neither set

---

## Phase 2 — Discord Connector File Uploads

### M3 — connector-discord: download attachments to uploads/
- [ ] In `_handle_message()`, detect `message.attachments` (Discord.py's Attachment objects)
- [ ] For each attachment: download to `uploads/{filename}` in the session workspace
  - Path: `~/.kiso/sessions/{session}/uploads/{filename}`
  - Handle filename collisions (append counter)
  - Respect size limits (skip files > 25MB with a warning in message text)
- [ ] Append attachment info to the forwarded message content, e.g.: `\n\n[Attached: image.png, report.pdf]`
- [ ] The planner will see the attachment note and can decide to use docreader/browser/exec to process them
- [ ] Add unit tests with mocked Discord attachments

### M4 — connector-discord: mention uploaded files in message
- [ ] Ensure the message text includes `[Uploaded files: ...]` so the planner knows files are available in `uploads/`
- [ ] Test: message with 1 attachment, message with multiple, message with oversized file (skipped with note)

---

## Phase 3 — Plugin Compliance Cleanup

### M5 — Add missing README.md files
- [ ] tool-moltbook: create README.md (description, setup, usage, env vars)
- [ ] tool-gworkspace: create README.md (description, setup, auth flow, usage, env vars)

### M6 — Add missing LICENSE files
- [ ] tool-moltbook: add MIT LICENSE
- [ ] tool-gworkspace: add MIT LICENSE

### M7 — Add missing/empty deps.sh files
- [ ] tool-websearch: create empty deps.sh (no system deps needed)
- [ ] tool-aider: create deps.sh (git is already a bin dep but no system install needed in Docker)
- [ ] tool-moltbook: create empty deps.sh
- [ ] connector-discord: create empty deps.sh

---

## Phase 4 — Document Reader Tool

### M8 — tool-docreader: create plugin
- [ ] Create `plugins/tool-docreader/` with standard structure
- [ ] kiso.toml: type=tool, name=docreader, args: action (read/extract), file_path, pages (optional)
- [ ] run.py: read PDF (via pypdf), DOCX (via python-docx), CSV, XLSX (via openpyxl), plain text
- [ ] Output: extracted text content
- [ ] deps.sh: install any needed system deps (poppler-utils for PDF if needed)
- [ ] Tests, README, LICENSE

---

## Phase 5 — Additional Presets (deferred)

### M9 — `developer` preset
Tools: aider, websearch, browser. Behaviors: TDD, code review, git workflow.
Deferred: behaviors are opinionated — need user feedback on basic first.

### M10 — `researcher` preset
Tools: websearch, browser. Behaviors: source verification, structured synthesis.
Deferred: niche use case.

### M11 — `assistant` preset
Tools: gworkspace, websearch, browser. Behaviors: email drafts, calendar confirmation.
Deferred: depends on gworkspace tool maturity.

---

## MD Skills — status: on hold

MD skills (lightweight planner instructions in .md files) work for **workflow guidance**
(plan structure, task ordering, strategy) but NOT for **content quality** (the planner
delegates content generation to worker/tools which don't see skills).

The infrastructure exists and costs nothing when unused. No skills will be created
speculatively — only when a real use case demonstrates that a skill improves plan
quality for a specific workflow.
