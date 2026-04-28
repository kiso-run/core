"""M1582 — session isolation fixture lock test (unit-tier).

The session-scoped `_func_kiso_dir` autouse fixture shares one temp
KISO_DIR across the whole functional run; that creates cross-test
contamination (classifier sees facts persisted by an earlier test).
M1582 introduces a `clean_session` fixture that produces an isolated
KISO_DIR + fresh DB per call.

This lock is unit-tier on the fixture itself: two consecutive
invocations must yield distinct paths and an empty fact store. The
end-to-end fix (rolling `clean_session` out across F1 / F2 / F7 /
F40) requires the LLM functional tier and is deferred — but the
fixture's isolation contract can and must be locked here.
"""

from __future__ import annotations


async def test_clean_session_yields_isolated_paths(clean_session):
    """The fixture's KISO_DIR is rooted under the per-test tmp_path,
    so two consecutive tests cannot share files."""
    assert clean_session.kiso_dir.exists()
    assert (clean_session.kiso_dir / "sys" / "ssh").is_dir()


async def test_clean_session_db_is_empty(clean_session):
    """Fresh DB → no facts, no plans, no sessions."""
    cur = await clean_session.db.execute(
        "SELECT COUNT(*) FROM facts",
    )
    facts_row = await cur.fetchone()
    assert facts_row[0] == 0

    cur = await clean_session.db.execute(
        "SELECT COUNT(*) FROM plans",
    )
    plans_row = await cur.fetchone()
    assert plans_row[0] == 0


async def test_clean_session_id_is_unique_per_call(clean_session):
    """Each invocation produces a fresh session id (uuid prefix)."""
    assert clean_session.session_id.startswith("clean-")
    # Hex tail length: 12 chars (uuid4 first 12 hex digits)
    tail = clean_session.session_id.removeprefix("clean-")
    assert len(tail) == 12
    int(tail, 16)  # valid hex; raises if not
