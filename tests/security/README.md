# Security regressions

One test file per concern from the pre-release security audit. Each
test encodes an invariant the runtime must keep holding. A passing
test means the invariant is verified; a failing test means either
the code regressed or the invariant needs to be re-examined.

Run: `uv run pytest tests/security/ -q`
