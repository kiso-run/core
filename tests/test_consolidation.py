"""Tests for the consolidator (periodic knowledge quality review)."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from kiso.brain import (
    ConsolidatorError,
    CONSOLIDATOR_SCHEMA,
    apply_consolidation_result,
    build_consolidator_messages,
    run_consolidator,
    validate_consolidator,
    _group_facts_by_entity,
)
from kiso.store import (
    delete_facts,
    get_facts,
    get_kv,
    init_db,
    save_fact,
    set_kv,
    update_fact_content,
)
from kiso.worker.loop import _maybe_run_consolidation, _LAST_CONSOLIDATION_KV_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    """Create a fresh in-memory-like SQLite database."""
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


def _make_config(overrides: dict | None = None):
    """Build a minimal Config-like object for testing."""
    from kiso.config import load_config
    from pathlib import Path
    from unittest.mock import patch as _p
    import tempfile, os

    settings = {
        "consolidation_enabled": True,
        "consolidation_interval_hours": 24,
        "consolidation_min_facts": 20,
        "knowledge_max_facts": 50,
        "max_llm_retries": 1,
        "max_validation_retries": 1,
        "llm_timeout": 5,
        "briefer_enabled": False,
    }
    if overrides:
        settings.update(overrides)

    class FakeConfig:
        def __init__(self):
            self.settings = settings
            self.models = {"consolidator": "test-consolidator"}
    return FakeConfig()


# ---------------------------------------------------------------------------
# validate_consolidator
# ---------------------------------------------------------------------------

class TestValidateConsolidator:

    def test_valid_result(self):
        ids = {1, 2, 3, 4, 5}
        result = {
            "delete": [1, 2],
            "update": [{"id": 3, "content": "merged fact"}],
            "keep": [4, 5],
        }
        assert validate_consolidator(result, ids) == []

    def test_missing_ids(self):
        ids = {1, 2, 3}
        result = {"delete": [1], "update": [], "keep": [2]}
        errors = validate_consolidator(result, ids)
        assert any("Missing fact IDs" in e for e in errors)

    def test_extra_ids(self):
        ids = {1, 2}
        result = {"delete": [], "update": [], "keep": [1, 2, 99]}
        errors = validate_consolidator(result, ids)
        assert any("Unknown fact IDs" in e for e in errors)

    def test_overlap_delete_update(self):
        ids = {1, 2}
        result = {
            "delete": [1],
            "update": [{"id": 1, "content": "x"}],
            "keep": [2],
        }
        errors = validate_consolidator(result, ids)
        assert any("both delete and update" in e for e in errors)

    def test_overlap_delete_keep(self):
        ids = {1, 2}
        result = {"delete": [1], "update": [], "keep": [1, 2]}
        errors = validate_consolidator(result, ids)
        assert any("both delete and keep" in e for e in errors)

    def test_overlap_update_keep(self):
        ids = {1, 2}
        result = {
            "delete": [],
            "update": [{"id": 1, "content": "x"}],
            "keep": [1, 2],
        }
        errors = validate_consolidator(result, ids)
        assert any("both update and keep" in e for e in errors)

    def test_empty_update_content(self):
        ids = {1, 2}
        result = {
            "delete": [],
            "update": [{"id": 1, "content": ""}],
            "keep": [2],
        }
        errors = validate_consolidator(result, ids)
        assert any("empty content" in e for e in errors)

    def test_all_kept(self):
        ids = {1, 2, 3}
        result = {"delete": [], "update": [], "keep": [1, 2, 3]}
        assert validate_consolidator(result, ids) == []


# ---------------------------------------------------------------------------
# build_consolidator_messages
# ---------------------------------------------------------------------------

class TestBuildConsolidatorMessages:

    def test_groups_by_entity(self):
        facts_by_entity = {
            "Python": [
                {"id": 1, "content": "Python is a language"},
                {"id": 2, "content": "Python 3.12 is latest"},
            ],
            "(no entity)": [
                {"id": 3, "content": "User prefers dark mode"},
            ],
        }
        msgs = build_consolidator_messages(facts_by_entity)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "### (no entity)" in msgs[1]["content"]
        assert "### Python" in msgs[1]["content"]
        assert "[1]" in msgs[1]["content"]
        assert "[3]" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# _group_facts_by_entity
# ---------------------------------------------------------------------------

class TestGroupFactsByEntity:

    def test_groups_correctly(self):
        facts = [
            {"id": 1, "content": "a", "entity_id": 10},
            {"id": 2, "content": "b", "entity_id": 10},
            {"id": 3, "content": "c", "entity_id": None},
        ]
        entities = [{"id": 10, "name": "Python"}]
        result = _group_facts_by_entity(facts, entities)
        assert len(result["Python"]) == 2
        assert len(result["(no entity)"]) == 1


# ---------------------------------------------------------------------------
# apply_consolidation_result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestApplyConsolidationResult:

    async def test_deletes_facts(self, db):
        fid1 = await save_fact(db, "fact one is important", "test", category="general")
        fid2 = await save_fact(db, "fact two is important", "test", category="general")
        result = {"delete": [fid1], "update": [], "keep": [fid2]}
        await apply_consolidation_result(db, result)
        remaining = await get_facts(db, is_admin=True)
        assert len(remaining) == 1
        assert remaining[0]["id"] == fid2

    async def test_updates_facts(self, db):
        fid = await save_fact(db, "old content for fact", "test", category="general")
        result = {
            "delete": [],
            "update": [{"id": fid, "content": "new merged content"}],
            "keep": [],
        }
        await apply_consolidation_result(db, result)
        facts = await get_facts(db, is_admin=True)
        assert len(facts) == 1
        assert facts[0]["content"] == "new merged content"

    async def test_empty_result_noop(self, db):
        fid = await save_fact(db, "unchanged fact content", "test", category="general")
        result = {"delete": [], "update": [], "keep": [fid]}
        await apply_consolidation_result(db, result)
        facts = await get_facts(db, is_admin=True)
        assert len(facts) == 1
        assert facts[0]["content"] == "unchanged fact content"


# ---------------------------------------------------------------------------
# store: kv helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestKvHelpers:

    async def test_get_kv_missing(self, db):
        assert await get_kv(db, "nonexistent") is None

    async def test_set_and_get_kv(self, db):
        await set_kv(db, "test_key", "test_value")
        assert await get_kv(db, "test_key") == "test_value"

    async def test_set_kv_upsert(self, db):
        await set_kv(db, "k", "v1")
        await set_kv(db, "k", "v2")
        assert await get_kv(db, "k") == "v2"


# ---------------------------------------------------------------------------
# store: update_fact_content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestUpdateFactContent:

    async def test_updates_content(self, db):
        fid = await save_fact(db, "original content here", "test", category="general")
        await update_fact_content(db, fid, "updated content here")
        facts = await get_facts(db, is_admin=True)
        assert facts[0]["content"] == "updated content here"


# ---------------------------------------------------------------------------
# _maybe_run_consolidation -- gate logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMaybeRunConsolidation:

    async def test_skips_when_too_recent(self, db):
        """Consolidation should not run if last consolidation was within interval."""
        config = _make_config({"consolidation_interval_hours": 24, "consolidation_min_facts": 1})
        # Set last consolidation to now
        await set_kv(db, _LAST_CONSOLIDATION_KV_KEY, str(time.time()))
        # Add enough facts
        for i in range(5):
            await save_fact(db, f"fact number {i} for testing", "test", category="general")

        with patch("kiso.worker.loop.run_consolidator", new_callable=AsyncMock) as mock_consolidator:
            await _maybe_run_consolidation(db, config, "test-session", 5)
            mock_consolidator.assert_not_called()

    async def test_skips_when_too_few_facts(self, db):
        """Consolidation should not run if fewer than consolidation_min_facts."""
        config = _make_config({"consolidation_interval_hours": 0, "consolidation_min_facts": 100})
        # No last consolidation time, but too few facts
        await save_fact(db, "only one fact here for test", "test", category="general")

        with patch("kiso.worker.loop.run_consolidator", new_callable=AsyncMock) as mock_consolidator:
            await _maybe_run_consolidation(db, config, "test-session", 5)
            mock_consolidator.assert_not_called()

    async def test_runs_when_gates_pass(self, db):
        """Consolidation should run when enough time has passed and enough facts exist."""
        config = _make_config({"consolidation_interval_hours": 1, "consolidation_min_facts": 2})
        # Last consolidation was long ago
        await set_kv(db, _LAST_CONSOLIDATION_KV_KEY, str(time.time() - 7200))
        # Enough facts
        for i in range(3):
            await save_fact(db, f"fact number {i} is long enough", "test", category="general")

        consolidation_result = {"delete": [], "update": [], "keep": []}
        with patch("kiso.worker.loop.run_consolidator", new_callable=AsyncMock, return_value=consolidation_result) as mock_consolidator:
            with patch("kiso.worker.loop.apply_consolidation_result", new_callable=AsyncMock) as mock_apply:
                await _maybe_run_consolidation(db, config, "test-session", 5)
                mock_consolidator.assert_called_once()
                mock_apply.assert_called_once_with(db, consolidation_result)

    async def test_runs_when_never_consolidated(self, db):
        """Consolidation should run when no last_consolidation_time exists."""
        config = _make_config({"consolidation_interval_hours": 1, "consolidation_min_facts": 2})
        for i in range(3):
            await save_fact(db, f"fact number {i} is long enough", "test", category="general")

        consolidation_result = {"delete": [], "update": [], "keep": []}
        with patch("kiso.worker.loop.run_consolidator", new_callable=AsyncMock, return_value=consolidation_result):
            with patch("kiso.worker.loop.apply_consolidation_result", new_callable=AsyncMock):
                await _maybe_run_consolidation(db, config, "test-session", 5)
                # Should have set the timestamp
                raw = await get_kv(db, _LAST_CONSOLIDATION_KV_KEY)
                assert raw is not None
                assert float(raw) > 0

    async def test_handles_consolidator_error(self, db):
        """ConsolidatorError should be caught, not propagated."""
        config = _make_config({"consolidation_interval_hours": 1, "consolidation_min_facts": 1})
        await save_fact(db, "fact for consolidator error test", "test", category="general")

        with patch("kiso.worker.loop.run_consolidator", new_callable=AsyncMock, side_effect=ConsolidatorError("bad")):
            # Should not raise
            await _maybe_run_consolidation(db, config, "test-session", 5)

    async def test_handles_timeout(self, db):
        """Timeout should be caught, not propagated."""
        config = _make_config({"consolidation_interval_hours": 1, "consolidation_min_facts": 1})
        await save_fact(db, "fact for timeout testing", "test", category="general")

        async def slow_consolidator(*a, **kw):
            await asyncio.sleep(100)

        with patch("kiso.worker.loop.run_consolidator", new_callable=AsyncMock, side_effect=slow_consolidator):
            # timeout=0.01 should trigger TimeoutError quickly
            await _maybe_run_consolidation(db, config, "test-session", 0)
