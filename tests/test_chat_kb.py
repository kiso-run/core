"""M374: Integration tests — chat_kb flow + messenger sanitization."""

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
from kiso.worker.loop import _fast_path_chat, _msg_task


def _full_models(**overrides):
    defaults = {
        "planner": "gpt-4", "worker": "gpt-4", "reviewer": "gpt-4",
        "messenger": "gpt-4", "briefer": "gpt-4", "summarizer": "gpt-4",
        "curator": "gpt-4", "classifier": "gpt-4",
    }
    defaults.update(overrides)
    return defaults


def _config(**settings_overrides):
    settings = {
        "context_messages": "3", "summarize_threshold": "999",
        "summarize_messages_limit": "50", "knowledge_max_facts": "200",
        "max_replan_depth": "2", "max_llm_retries": "3",
        "max_validation_retries": "3", "worker_idle_timeout": "0.01",
        "classifier_timeout": "5", "llm_timeout": "30",
        "planner_timeout": "30", "messenger_timeout": "10",
        "briefer_enabled": "true", "bot_name": "Kiso",
    }
    settings.update(settings_overrides)
    return Config(
        tokens={"cli": "tok"},
        providers={"openrouter": Provider(base_url="https://api.example.com/v1")},
        users={},
        models=_full_models(),
        settings=settings,
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
                    "modules": [], "skills": [],
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
                    "modules": [], "skills": [],
                    "context": "General chat.",
                    "output_indices": [], "relevant_tags": [],
                    "relevant_entities": [],
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
                    "modules": [], "skills": [], "context": "",
                    "output_indices": [], "relevant_tags": [],
                    "relevant_entities": [],
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
    """M373 output-backed learning validation in realistic scenarios."""

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
