"""M622: Interactive test infrastructure.

Provides the ``HumanRelay`` fixture that bridges agent output and real human
input during tests requiring manual actions (CAPTCHA, OAuth, SSH key deploy).

These tests are NEVER run in CI — they require a human at the terminal.
Gated by ``--interactive`` flag.
"""

from __future__ import annotations

import pytest


class HumanRelay:
    """Bridges agent and human during interactive tests.

    Usage in tests::

        async def test_ssh_deploy(human_relay):
            result = await human_relay.send("genera una chiave SSH")
            assert result.success

    The relay sends a message through the functional test pipeline, checks
    if the agent is stuck or waiting for human action, and prompts the
    terminal operator if needed.
    """

    def __init__(self, run_message_fn):
        self._run = run_message_fn
        self.transcript: list[dict] = []
        self.max_rounds: int = 5

    async def send(self, content: str, **kw):
        """Send a message, intercept if agent asks for human action.

        Returns the final FunctionalResult after all human interactions
        are resolved (or max_rounds is reached).
        """
        result = await self._run(content, **kw)
        self.transcript.append({"role": "agent", "content": result.msg_output})

        rounds = 0
        while self._needs_human_action(result) and rounds < self.max_rounds:
            rounds += 1
            human_input = self._prompt_human(result.msg_output, rounds)
            if human_input is None:
                break  # Human chose to abort
            self.transcript.append({"role": "human", "content": human_input})
            result = await self._run(human_input, **kw)
            self.transcript.append({"role": "agent", "content": result.msg_output})

        return result

    def _needs_human_action(self, result) -> bool:
        """Detect if the agent is waiting for human action."""
        # Check for stuck tasks
        if hasattr(result, "tasks"):
            for task in result.tasks:
                if isinstance(task, dict) and task.get("status") == "stuck":
                    return True
        # Check if the agent message explicitly asks for user action
        msg = (result.msg_output or "").lower()
        if any(phrase in msg for phrase in [
            "please complete", "you need to", "waiting for you",
            "add the key", "solve the captcha", "authorize",
        ]):
            return True
        return False

    def _prompt_human(self, agent_msg: str, round_num: int) -> str | None:
        """Show agent message, wait for human input at terminal."""
        print()
        print("=" * 60)
        print(f"  HUMAN ACTION REQUIRED (round {round_num})")
        print("=" * 60)
        print()
        print(agent_msg or "(no message from agent)")
        print()
        print("-" * 60)
        try:
            response = input("Your response (or 'abort' to stop): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if response.lower() == "abort":
            return None
        return response or "done"


@pytest.fixture()
def human_relay(request):
    """Provide a HumanRelay wired to the functional test pipeline.

    Requires the ``run_message`` fixture from functional/conftest.py.
    If run_message is not available (e.g., running outside Docker),
    the fixture skips the test.
    """
    run_message = request.getfixturevalue("run_message")
    return HumanRelay(run_message)
