"""Integration tests — chat_kb flow + messenger sanitization."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiso.brain import (
    _sanitize_messenger_output,
    clean_learn_items,
)
from kiso.config import Config, Provider
from kiso.store import (
    create_session,
    find_or_create_entity,
    init_db,
    save_fact,
)
from kiso.worker.loop import (
    _CHAT_KB_FALLBACK_MSGS,
    _PLAN_TO_CHAT_KB_PROMOTE_MSGS,
    _chat_kb_preflight_fallback,
    _fast_path_chat,
    _has_imperative_prefix,
    _looks_like_question,
    _msg_task,
    _plan_chat_kb_preflight_promotion,
)
from tests.conftest import full_settings, full_models


def _config(**settings_overrides):
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=full_models(),
        settings=full_settings(briefer_enabled=True, bot_name="Kiso", **settings_overrides),
        raw={},
    )


class TestChatKBEntityFlow:
    """chat_kb flow: classifier → briefer → messenger, with entity facts."""

    @pytest.fixture()
    async def db(self, tmp_path):
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        yield conn
        await conn.close()

    async def test_full_chat_kb_with_entity_facts(self, db):
        """chat_kb + entity: briefer selects entity → messenger gets facts."""
        config = _config()
        eid = await find_or_create_entity(db, "self", "system")
        await save_fact(
            db, "Instance SSH key (at ~/.kiso/sys/ssh/id_ed25519.pub): ssh-ed25519 AAAA",
            source="system", session=None, category="system",
            tags=["ssh", "credentials"], entity_id=eid,
        )

        messenger_msgs = []

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps({
                    "modules": [], "tools": [], "exclude_recipes": [],
                    "context": "User asks about SSH key.",
                    "output_indices": [], "relevant_tags": ["ssh"],
                    "relevant_entities": ["self"],
                })
            messenger_msgs.append(messages)
            return "Your SSH key is ssh-ed25519 AAAA"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm):
            result = await _msg_task(config, db, "sess1", "What is my SSH key?")

        assert "ssh-ed25519 AAAA" in result
        # Messenger received entity facts in context
        user_content = messenger_msgs[0][1]["content"]
        assert "ssh-ed25519 AAAA" in user_content
        assert "Relevant Facts" in user_content

    async def test_chat_kb_no_entities_still_works(self, db):
        """chat_kb with no relevant entities → messenger runs normally."""
        config = _config()

        async def _fake_llm(cfg, role, messages, **kw):
            if role == "briefer":
                return json.dumps({
                    "modules": [], "tools": [],
                    "context": "General chat.",
                    "output_indices": [], "relevant_tags": [],
                    "exclude_recipes": [], "relevant_entities": [],
                })
            return "Hello! How can I help?"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm):
            result = await _msg_task(config, db, "sess1", "Ciao!")

        assert result == "Hello! How can I help?"

    async def test_chat_kb_via_fast_path_no_planner_called(self, db, tmp_path):
        """_fast_path_chat never invokes planner — only briefer + messenger."""
        config = _config()
        roles_called = []

        async def _fake_llm(cfg, role, messages, **kw):
            roles_called.append(role)
            if role == "briefer":
                return json.dumps({
                    "modules": [], "tools": [], "context": "",
                    "output_indices": [], "relevant_tags": [],
                    "exclude_recipes": [], "relevant_entities": [],
                })
            return "Hi there"

        with patch("kiso.brain.call_llm", side_effect=_fake_llm), \
             patch("kiso.worker.loop.collect_deploy_secrets", return_value={}), \
             patch("kiso.worker.utils.KISO_DIR", tmp_path), \
             patch("kiso.worker.loop.KISO_DIR", tmp_path), \
             patch("kiso.worker.loop._check_disk_limit", return_value=None):
            await _fast_path_chat(db, config, "sess1", 1, "hello")

        assert "planner" not in roles_called
        assert "reviewer" not in roles_called
        assert "messenger" in roles_called


class TestChatKBPreflightFallback:
    """M1291: pre-flight chat_kb facts check + investigate fallback."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_plan
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        plan_id = await create_plan(conn, "sess1", 1, "Thinking...")
        conn._test_plan_id = plan_id  # type: ignore[attr-defined]
        yield conn
        await conn.close()

    async def _list_msg_tasks(self, db, plan_id):
        from kiso.store import get_tasks_for_plan
        tasks = await get_tasks_for_plan(db, plan_id)
        return [t for t in tasks if t.get("type") == "msg"]

    async def test_preflight_returns_false_when_facts_exist(self, db):
        """Facts present in DB → no fallback, no transition task created."""
        config = _config()
        eid = await find_or_create_entity(db, "self", "system")
        await save_fact(
            db, "Database hostname is db.internal.example.com",
            source="system", session=None, category="system",
            tags=["database"], entity_id=eid,
        )
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _chat_kb_preflight_fallback(
                db, config, "sess1", plan_id,
                content="What is the database hostname?",
                user_lang="English",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_preflight_triggers_fallback_on_empty_db(self, db):
        """Empty KB → fallback signaled, transition msg task persisted."""
        config = _config()
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _chat_kb_preflight_fallback(
                db, config, "sess1", plan_id,
                content="What is the current uptime?",
                user_lang="English",
            )

        assert triggered is True
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert len(msg_tasks) == 1
        assert _CHAT_KB_FALLBACK_MSGS["English"] in (msg_tasks[0].get("output") or "")

    async def test_preflight_skipped_when_content_empty(self, db):
        """Empty content → no keywords → return False, no fallback."""
        config = _config()
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _chat_kb_preflight_fallback(
                db, config, "sess1", plan_id,
                content="",
                user_lang="English",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_preflight_search_error_does_not_fallback(self, db):
        """search_facts_scored raises → return False, no transition task."""
        config = _config()
        plan_id = db._test_plan_id

        async def _boom(*a, **kw):
            raise RuntimeError("simulated DB failure")

        with patch("kiso.worker.loop.search_facts_scored", side_effect=_boom), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _chat_kb_preflight_fallback(
                db, config, "sess1", plan_id,
                content="What is the database hostname?",
                user_lang="English",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_transition_message_italian(self, db):
        """user_lang='Italian' → italian string used."""
        config = _config()
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _chat_kb_preflight_fallback(
                db, config, "sess1", plan_id,
                content="qual è l'uptime del sistema?",
                user_lang="Italian",
            )

        assert triggered is True
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert len(msg_tasks) == 1
        output = msg_tasks[0].get("output") or ""
        assert _CHAT_KB_FALLBACK_MSGS["Italian"] in output
        assert "knowledge base" in output  # appears in both languages, sanity

    async def test_transition_message_unknown_lang_defaults_english(self, db):
        """Unknown user_lang → English fallback."""
        config = _config()
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _chat_kb_preflight_fallback(
                db, config, "sess1", plan_id,
                content="What is the current uptime?",
                user_lang="Klingon",
            )

        assert triggered is True
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        output = msg_tasks[0].get("output") or ""
        assert _CHAT_KB_FALLBACK_MSGS["English"] in output


class TestPlanChatKbPromotion:
    """M1299: pre-flight promotion plan/investigate → chat_kb when KB has matches."""

    @pytest.fixture()
    async def db(self, tmp_path):
        from kiso.store import create_plan
        conn = await init_db(tmp_path / "test.db")
        await create_session(conn, "sess1")
        plan_id = await create_plan(conn, "sess1", 1, "Thinking...")
        conn._test_plan_id = plan_id  # type: ignore[attr-defined]
        yield conn
        await conn.close()

    async def _list_msg_tasks(self, db, plan_id):
        from kiso.store import get_tasks_for_plan
        tasks = await get_tasks_for_plan(db, plan_id)
        return [t for t in tasks if t.get("type") == "msg"]

    # ---- imperative / question gates (pure helpers) -----------------

    def test_question_helper_detects_question_mark(self):
        assert _looks_like_question("che framework usa il progetto?") is True
        assert _looks_like_question("what is X?") is True

    def test_question_helper_detects_question_word_no_qmark(self):
        assert _looks_like_question("cosa sai di flask") is True
        assert _looks_like_question("what is flask") is True
        assert _looks_like_question("qué es flask") is True

    def test_question_helper_rejects_declarative(self):
        assert _looks_like_question("il progetto è in flask") is False
        assert _looks_like_question("flask is great") is False
        assert _looks_like_question("") is False

    def test_imperative_helper_detects_en_it_es(self):
        assert _has_imperative_prefix("delete the flask config") is True
        assert _has_imperative_prefix("install flask") is True
        assert _has_imperative_prefix("ricordati che usa flask") is True
        assert _has_imperative_prefix("crea un file") is True
        assert _has_imperative_prefix("ejecuta el script") is True

    def test_imperative_helper_rejects_questions(self):
        assert _has_imperative_prefix("che framework usa?") is False
        assert _has_imperative_prefix("what framework?") is False
        assert _has_imperative_prefix("") is False

    # ---- promotion gate -----------------------------------------------

    async def test_promotes_question_when_kb_has_match(self, db):
        """Question + KB match + non-imperative → promotion fires."""
        config = _config()
        eid = await find_or_create_entity(db, "flask", "tool")
        await save_fact(
            db, "Flask is a lightweight Python web framework",
            source="curator", session=None, category="general",
            tags=["python", "web"], entity_id=eid,
        )
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="che framework usa il progetto corrente?",
                user_lang="Italian",
            )

        assert triggered is True
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert len(msg_tasks) == 1
        assert _PLAN_TO_CHAT_KB_PROMOTE_MSGS["Italian"] in (msg_tasks[0].get("output") or "")

    async def test_does_not_promote_imperative_even_if_kb_match(self, db):
        """Imperative prefix → never promote even when KB matches."""
        config = _config()
        eid = await find_or_create_entity(db, "flask", "tool")
        await save_fact(
            db, "Flask is a Python web framework",
            source="curator", session=None, category="general",
            tags=["python"], entity_id=eid,
        )
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="delete the flask config?",
                user_lang="English",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_does_not_promote_when_kb_empty(self, db):
        """No facts in KB → no promotion."""
        config = _config()
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="what is the current framework?",
                user_lang="English",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_does_not_promote_declarative(self, db):
        """No question mark + no question word → no promotion."""
        config = _config()
        eid = await find_or_create_entity(db, "flask", "tool")
        await save_fact(
            db, "Flask is a Python web framework",
            source="curator", session=None, category="general",
            tags=["python"], entity_id=eid,
        )
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="il progetto è fatto in flask",
                user_lang="Italian",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_does_not_promote_when_content_empty(self, db):
        """Empty content → no promotion."""
        config = _config()
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="",
                user_lang="English",
            )

        assert triggered is False

    async def test_search_error_does_not_promote(self, db):
        """search_facts_scored raises → return False, no transition task."""
        config = _config()
        plan_id = db._test_plan_id

        async def _boom(*a, **kw):
            raise RuntimeError("simulated DB failure")

        with patch("kiso.worker.loop.search_facts_scored", side_effect=_boom), \
             patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="what is flask?",
                user_lang="English",
            )

        assert triggered is False
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert msg_tasks == []

    async def test_promotion_persists_assistant_message(self, db):
        """Promoted message is saved to conversation history exactly once."""
        from kiso.store import get_recent_messages

        config = _config()
        eid = await find_or_create_entity(db, "flask", "tool")
        await save_fact(
            db, "Flask is a Python web framework",
            source="curator", session=None, category="general",
            tags=["python"], entity_id=eid,
        )
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="what framework do we use?",
                user_lang="English",
            )

        msgs = await get_recent_messages(db, "sess1", limit=10)
        assistant_msgs = [
            m for m in msgs
            if m.get("role") == "assistant"
            and _PLAN_TO_CHAT_KB_PROMOTE_MSGS["English"] in (m.get("content") or "")
        ]
        assert len(assistant_msgs) == 1

    async def test_unknown_lang_defaults_english(self, db):
        """Unknown user_lang → English transition message."""
        config = _config()
        eid = await find_or_create_entity(db, "flask", "tool")
        await save_fact(
            db, "Flask is a Python web framework",
            source="curator", session=None, category="general",
            tags=["python"], entity_id=eid,
        )
        plan_id = db._test_plan_id

        with patch("kiso.worker.loop._deliver_webhook_if_configured", return_value=None):
            triggered = await _plan_chat_kb_preflight_promotion(
                db, config, "sess1", plan_id,
                content="what framework?",
                user_lang="Klingon",
            )

        assert triggered is True
        msg_tasks = await self._list_msg_tasks(db, plan_id)
        assert _PLAN_TO_CHAT_KB_PROMOTE_MSGS["English"] in (msg_tasks[0].get("output") or "")


class TestMessengerSanitization:
    """Messenger output sanitization strips hallucinated tool markup."""

    def test_tool_call_block_stripped(self):
        text = 'Hello!\n<tool_call name="search">{"q":"test"}</tool_call>\nHow are you?'
        result = _sanitize_messenger_output(text)
        assert "<tool_call" not in result
        assert "Hello!" in result
        assert "How are you?" in result

    def test_function_call_block_stripped(self):
        text = "Sure!\n<function_call>do_something()</function_call>"
        result = _sanitize_messenger_output(text)
        assert "<function_call" not in result
        assert "Sure!" in result

    def test_orphaned_tags_stripped(self):
        text = "Result: </tool_call> some text"
        result = _sanitize_messenger_output(text)
        assert "</tool_call>" not in result
        assert "some text" in result

    def test_normal_output_unchanged(self):
        text = "Your SSH key is ssh-ed25519 AAAA at ~/.kiso/sys/ssh/id_ed25519.pub"
        result = _sanitize_messenger_output(text)
        assert result == text

    def test_empty_output(self):
        result = _sanitize_messenger_output("")
        assert result == ""


class TestOutputBackedLearningIntegration:
    """output-backed learning validation in realistic scenarios."""

    def test_negative_claim_about_present_item_filtered(self):
        """Reviewer says "not stated" but subject IS in output."""
        items = [
            "The kernel release was not stated in the system output",
            "Project uses Flask framework for serving",
        ]
        output = "Linux 6.1.0-20-amd64 #1 SMP kernel release"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1
        assert "Flask" in result[0]

    def test_legitimate_negative_preserved(self):
        """Negative claim where subject truly absent → kept."""
        items = ["Terraform toolchain not available on this host"]
        output = "total 0\nno matching packages found"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1

    def test_combined_filters(self):
        """Short + transient + contradicted all filtered in one pass."""
        items = [
            "too short",                                              # <15 chars
            "nginx installed successfully on the server",             # transient
            "python package not found on the system",                 # contradicted
            "Server runs Ubuntu 22.04 LTS with systemd init system", # valid
        ]
        output = "Python 3.11.2 is installed\npython3 /usr/bin/python3"
        result = clean_learn_items(items, task_output=output)
        assert len(result) == 1
        assert "Ubuntu" in result[0]
