# kiso-run/core — Project Instructions

Project-specific rules. Generic workflow rules live in the global
`~/.claude/CLAUDE.md`.

## Version metadata stays in sync with the active devplan

`pyproject.toml`'s `version = "X.Y.Z"` field is the **single source of
truth** for the package version. The installer (`install.sh`) reads it
directly via grep, `kiso/_version.py` reads it via `importlib.metadata`,
and every consumer (`kiso/api/runtime.py`, `cli/__init__.py`,
`tests/test_health.py`, `tests/test_cli.py::TestVersioning`) flows from
there.

The package version MUST track the active devplan version:

- Active devplan `devplan/v0.8.md` → `pyproject.toml` says `0.8.0`
- Active devplan `devplan/v0.9.md` → `pyproject.toml` says `0.9.0`
- ...

Whenever the active devplan changes — closing `vN`, opening
`vN+1-wip`, or moving milestones between version files — bump
`pyproject.toml` in the **same** change set, then run `uv lock` so
`uv.lock` agrees with the manifest. Do not split the bump across
multiple commits and do not "do it later".

If you ever notice the two have drifted (devplan version ≠ package
version), flag it immediately and propose the fix as a milestone.
The drift is always a bug, never intentional. M1266 (2026-04-08)
exists specifically because this was forgotten when v0.8 closed and
the project ran at `0.7.0` for the entire v0.8 cycle.

## Devplan layout

- `DEVPLAN.md` is a pointer to the active version file in `devplan/`
- Active devplan: append new milestones here, with `M` prefix and
  `## Phase N — ...` section headings (see existing structure)
- `devplan/vN+1-wip.md`: empty placeholder for the next cycle. Do
  NOT move work into it until the user explicitly says so
- Per the global rule "Respect version boundaries in devplans": never
  close a version or create a new version file unless the user says so

## Prompt-first debugging — kiso runs on prompts, not on luck

When an LLM-driven test (live or functional) fails reproducibly, the
**first** thing to check is the prompt that arrives at the model. We
use modern top-tier models — if one of them gets it wrong consistently,
the prompt that reached it is broken: incomplete, contradictory, or
overweight on a competing rule. "Model variance" / "LLM bias" is the
last resort, not the first explanation.

Workflow:

1. Dump the assembled prompt for the failing turn (system + user
   context). Add a temporary
   `if os.environ.get("KISO_DUMP_PLANNER_PROMPT"): write to disk`
   hook in `kiso/brain/planner.py:build_planner_messages` (or the
   role-specific equivalent) and re-run the failing test with the
   env var set.
2. Read the dumped prompt end-to-end as a single document. Look for:
   - Pairs of rules whose plain reading produces opposite outputs for
     the same input (e.g. "msg-only plans are rejected" vs Decision
     Tree branch 1 "msg-only plan with `needs_install`").
   - Authoritative context-injected sections (e.g. `## Install
     Routing: Mode: system_pkg`) that override the static prompt
     when the routing decision was actually wrong.
   - Generic rules (e.g. "natural-language WHAT, not HOW") that
     conflict with specific rules ("install `exec` detail must be
     the literal `kiso mcp install --from-url <url>`").
3. Fix the contradiction at the source. If a context-injection layer
   is producing wrong text (e.g. `_classify_install_mode` falling
   back to `system_pkg` for any "install <X>" without an explicit
   hint), fix the injector. If a static rule is too categorical,
   rephrase it.
4. Only after the prompt audit is clean — and the test still fails
   deterministically — consider higher-cost interventions
   (validator backstop, model upgrade, splitting the role into two
   specialized prompts).

This rule was learned the hard way during M1608 (planner trust-
persistence flake): five real prompt contradictions were buried in
a 22 KB prompt; each manual fix nudged the flake rate but only the
ones that surfaced via prompt-dump truly closed the regression.
"Modello top, prompt male" — Kiso runs on prompts; if a prompt
fails, debug the prompt, not the model.
