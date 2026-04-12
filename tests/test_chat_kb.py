"""Integration tests — chat_kb flow + messenger sanitization."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiso.brain import (
    EMOJI_STRIP_RE,
    _sanitize_messenger_output,
    clean_learn_items,
    strip_emoji,
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
    _chat_kb_preflight_fallback,
    _fast_path_chat,
    _msg_task,
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
                    "modules": [], "wrappers": [], "exclude_recipes": [],
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
                    "modules": [], "wrappers": [],
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
                    "modules": [], "wrappers": [], "context": "",
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


class TestClassifierIsAuthoritative:
    """The classifier's plan/investigate/chat_kb decision is authoritative.

    No pre-flight keyword match against the facts store may override a
    plan/investigate classification into chat_kb. Keyword matching is a
    weaker signal than full-message LLM semantic reasoning and must not
    reverse it. The symmetric fallback (chat_kb + empty KB → investigate)
    is preserved: it refines, not contradicts, the classifier.
    """

    def test_no_plan_to_chat_kb_promotion_symbols_in_loop(self):
        """Guard against regression: the removed promotion function and its
        supporting helpers/messages must not be re-introduced in the worker
        loop. A future refactor that re-adds any of these names restores
        the evidence-hierarchy inversion bug."""
        from kiso.worker import loop as loop_mod

        removed_names = [
            "_plan_chat_kb_preflight_promotion",
            "_PLAN_TO_CHAT_KB_PROMOTE_MSGS",
            "_looks_like_question",
            "_has_imperative_prefix",
            "_PLAN_PROMOTE_QUESTION_PREFIXES",
            "_PLAN_PROMOTE_IMPERATIVE_PREFIXES",
        ]
        found = [name for name in removed_names if hasattr(loop_mod, name)]
        assert not found, (
            f"Promotion-related symbols must not exist in kiso.worker.loop "
            f"(classifier is authoritative). Found: {found}"
        )

    def test_chat_kb_fallback_symbols_still_present(self):
        """The symmetric fallback (chat_kb + empty KB → investigate) is NOT
        removed. It is directionally safe — it refines the classifier
        decision, not overrides it."""
        from kiso.worker import loop as loop_mod

        assert hasattr(loop_mod, "_chat_kb_preflight_fallback")
        assert hasattr(loop_mod, "_CHAT_KB_FALLBACK_MSGS")


class TestMessengerSanitization:
    """Messenger output sanitization strips hallucinated wrapper markup."""

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

    def test_sanitizer_strips_emoji(self):
        """M1300: messenger sanitizer also strips emoji deterministically."""
        text = "**🎯 Interaction**\n💻 Code\n🌐 Net\n🛠 Wrappers\n🔬 Lab\n📚 Docs"
        result = _sanitize_messenger_output(text)
        for ch in ("🎯", "💻", "🌐", "🛠", "🔬", "📚"):
            assert ch not in result
        # surrounding text preserved
        assert "Interaction" in result
        assert "Code" in result


class TestStripEmoji:
    """M1300: deterministic emoji stripping for messenger output."""

    @pytest.mark.parametrize("emoji,name", [
        ("\U0001F3AF", "1F3AF target"),
        ("\U0001F4BB", "1F4BB laptop"),
        ("\U0001F310", "1F310 globe"),
        ("\U0001F6E0", "1F6E0 hammer-and-wrench"),
        ("\U0001F52C", "1F52C microscope"),
        ("\U0001F4DA", "1F4DA books"),
        ("\u2600", "2600 sun"),
        ("\u27BF", "27BF curly loop"),
        ("\U0001FA9F", "1FA9F window"),
    ])
    def test_strips_each_known_emoji(self, emoji, name):
        text = f"hello {emoji} world"
        assert strip_emoji(text) == "hello  world"

    def test_keeps_alphanumeric_and_punctuation(self):
        text = "Ciao! Sono Kiso, versione 0.8.0."
        assert strip_emoji(text) == text

    def test_keeps_markdown_structure(self):
        text = "**bold** _italic_ `code` # heading\n- bullet"
        assert strip_emoji(text) == text

    def test_handles_empty_and_none_safe(self):
        assert strip_emoji("") == ""

    def test_strips_multiple_in_a_row(self):
        text = "a🎯b💻c"
        assert strip_emoji(text) == "abc"

    def test_regex_parity_with_functional_test(self):
        """The functional test regex must match the production regex."""
        from tests.functional.test_knowledge import _EMOJI_RE
        # Both should match the same character set: probe with all six
        # emoji from the failing run.
        for ch in ("🎯", "💻", "🌐", "🛠", "🔬", "📚"):
            assert _EMOJI_RE.search(ch) is not None
            assert EMOJI_STRIP_RE.search(ch) is not None


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
