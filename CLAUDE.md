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
