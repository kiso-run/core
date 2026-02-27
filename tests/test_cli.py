"""Tests for kiso.cli — argument parsing, subcommand routing, and chat REPL."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from cli import (
    _ExitRepl,
    _POLL_EVERY,
    _SLASH_COMMANDS,
    _handle_slash,
    _msg_cmd,
    _poll_status,
    _save_readline_history,
    _setup_readline,
    build_parser,
    main,
    __version__,
)
from cli.render import TermCaps


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def plain_caps():
    """Non-color, non-TTY TermCaps for predictable output."""
    return TermCaps(color=False, unicode=False, width=120, height=50, tty=False)


# ── build_parser ──────────────────────────────────────────────


def test_build_parser_returns_argument_parser():
    assert isinstance(build_parser(), argparse.ArgumentParser)


# ── subcommand routing ────────────────────────────────────────


def test_serve_subcommand():
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.command == "serve"


def test_no_args_means_chat_mode():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.command is None


def test_session_flag():
    parser = build_parser()
    args = parser.parse_args(["--session", "foo"])
    assert args.session == "foo"
    assert args.command is None


def test_api_flag():
    parser = build_parser()
    args = parser.parse_args(["--api", "http://x:8000"])
    assert args.api == "http://x:8000"


def test_quiet_long_flag():
    parser = build_parser()
    args = parser.parse_args(["--quiet"])
    assert args.quiet is True


def test_quiet_short_flag():
    parser = build_parser()
    args = parser.parse_args(["-q"])
    assert args.quiet is True


@pytest.mark.parametrize("cmd", ["skill", "connector", "sessions", "env", "reset"])
def test_subcommand_parsed(cmd: str):
    parser = build_parser()
    args = parser.parse_args([cmd])
    assert args.command == cmd


# ── defaults ──────────────────────────────────────────────────


def test_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.session is None
    assert args.api == "http://localhost:8333"
    assert args.quiet is False


# ── _serve calls uvicorn ──────────────────────────────────────


def test_serve_calls_uvicorn():
    mock_cfg = MagicMock()
    mock_cfg.settings = {"host": "127.0.0.1", "port": 9000}

    with (
        patch("kiso.config.load_config", return_value=mock_cfg) as mock_load,
        patch("uvicorn.run") as mock_run,
    ):
        from cli import _serve

        _serve()

        mock_load.assert_called_once()
        mock_run.assert_called_once_with(
            "kiso.main:app", host="127.0.0.1", port=9000
        )


# ── sessions parsing ──────────────────────────────────────────


def test_sessions_default():
    parser = build_parser()
    args = parser.parse_args(["sessions"])
    assert args.command == "sessions"
    assert args.show_all is False


def test_sessions_all_flag():
    parser = build_parser()
    args = parser.parse_args(["sessions", "--all"])
    assert args.show_all is True


def test_sessions_short_flag():
    parser = build_parser()
    args = parser.parse_args(["sessions", "-a"])
    assert args.show_all is True


# ── env parsing ───────────────────────────────────────────────


def test_env_no_subcommand():
    parser = build_parser()
    args = parser.parse_args(["env"])
    assert args.command == "env"
    assert args.env_command is None


def test_env_set():
    parser = build_parser()
    args = parser.parse_args(["env", "set", "KEY", "VALUE"])
    assert args.env_command == "set"
    assert args.key == "KEY"
    assert args.value == "VALUE"


def test_env_get():
    parser = build_parser()
    args = parser.parse_args(["env", "get", "KEY"])
    assert args.env_command == "get"
    assert args.key == "KEY"


def test_env_list():
    parser = build_parser()
    args = parser.parse_args(["env", "list"])
    assert args.env_command == "list"


def test_env_delete():
    parser = build_parser()
    args = parser.parse_args(["env", "delete", "KEY"])
    assert args.env_command == "delete"
    assert args.key == "KEY"


def test_env_reload():
    parser = build_parser()
    args = parser.parse_args(["env", "reload"])
    assert args.env_command == "reload"


# ── reset parsing ────────────────────────────────────────────


def test_reset_no_subcommand():
    parser = build_parser()
    args = parser.parse_args(["reset"])
    assert args.command == "reset"
    assert args.reset_command is None


def test_reset_session_with_name():
    parser = build_parser()
    args = parser.parse_args(["reset", "session", "my-session"])
    assert args.reset_command == "session"
    assert args.name == "my-session"


def test_reset_session_default_name():
    parser = build_parser()
    args = parser.parse_args(["reset", "session"])
    assert args.reset_command == "session"
    assert args.name is None


def test_reset_session_yes_flag():
    parser = build_parser()
    args = parser.parse_args(["reset", "session", "--yes"])
    assert args.yes is True


def test_reset_session_y_flag():
    parser = build_parser()
    args = parser.parse_args(["reset", "session", "-y"])
    assert args.yes is True


def test_reset_knowledge():
    parser = build_parser()
    args = parser.parse_args(["reset", "knowledge"])
    assert args.reset_command == "knowledge"


def test_reset_all():
    parser = build_parser()
    args = parser.parse_args(["reset", "all"])
    assert args.reset_command == "all"


def test_reset_factory():
    parser = build_parser()
    args = parser.parse_args(["reset", "factory"])
    assert args.reset_command == "factory"


def test_reset_factory_yes():
    parser = build_parser()
    args = parser.parse_args(["reset", "factory", "--yes"])
    assert args.reset_command == "factory"
    assert args.yes is True


# ── _chat REPL ───────────────────────────────────────────────


def _mock_config(has_cli_token: bool = True):
    cfg = MagicMock()
    cfg.tokens = {"cli": "tok-abc"} if has_cli_token else {}
    return cfg


def _make_args(session=None, api="http://localhost:8333", quiet=False, user=None):
    return argparse.Namespace(session=session, api=api, quiet=quiet, user=user, command=None)


def test_chat_exits_on_exit_command(capsys):
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="host1"),
    ):
        _chat(_make_args())

    # No POST /msg should have been made
    mock_client.post.assert_not_called()
    mock_client.close.assert_called_once()


def test_chat_exits_on_keyboard_interrupt(capsys):
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=KeyboardInterrupt),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="host1"),
    ):
        _chat(_make_args())

    mock_client.close.assert_called_once()


def test_chat_exits_on_eof(capsys):
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=EOFError),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="host1"),
    ):
        _chat(_make_args())

    mock_client.close.assert_called_once()


def test_chat_missing_cli_token(capsys):
    from cli import _chat

    with (
        patch("kiso.config.load_config", return_value=_mock_config(has_cli_token=False)),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        pytest.raises(SystemExit, match="1"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "no 'cli' token" in out


def test_chat_default_session():
    from cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"queued": True, "message_id": 1, "session": "s"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="bob"),
        patch("socket.gethostname", return_value="myhost"),
        patch("cli._poll_status", return_value=0),
    ):
        _chat(_make_args(session=None))

    # Check the session passed to POST /msg
    post_call = mock_client.post.call_args
    assert post_call[1]["json"]["session"] == "myhost@bob"


def test_chat_connection_error(capsys):
    from cli import _chat

    mock_client = MagicMock()
    mock_client.post.side_effect = [
        httpx.ConnectError("refused"),
        None,  # won't be reached due to "exit"
    ]

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "cannot connect" in out


def test_chat_untrusted_message(capsys):
    from cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "queued": False,
        "untrusted": True,
        "message_id": 1,
        "session": "s",
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "not trusted" in out


def test_chat_cancel_on_ctrl_c_during_poll(capsys):
    from cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"queued": True, "message_id": 1, "session": "s"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
        patch("cli._poll_status", side_effect=KeyboardInterrupt),
    ):
        _chat(_make_args(session="mysess"))

    out = capsys.readouterr().out
    assert "Cancelling" in out
    # Verify cancel POST was attempted
    cancel_calls = [
        c for c in mock_client.post.call_args_list if "/cancel" in str(c)
    ]
    assert len(cancel_calls) == 1
    assert "mysess" in str(cancel_calls[0])


# ── _poll_status ─────────────────────────────────────────────


def test_poll_status_completes_on_plan_done(capsys, plain_caps):
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 42, "goal": "Do stuff", "status": "done"},
        "tasks": [
            {"id": 5, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Hello!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 5
    out = capsys.readouterr().out
    assert "Plan: Do stuff" in out
    assert "Bot:" in out
    assert "Hello!" in out
    # Separators present (ASCII dashes for plain_caps)
    assert "----" in out


def test_poll_status_renders_tasks(capsys, plain_caps):
    mock_client = MagicMock()
    # First poll: task running. Second poll: task done + plan done.
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "running"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "running", "output": ""},
        ],
    }
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "done"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "done",
             "output": "file.txt"},
            {"id": 4, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done."},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 4
    out = capsys.readouterr().out
    assert "exec: ls" in out
    assert "Bot:" in out
    assert "Done." in out


def test_poll_status_detects_replan(capsys, plain_caps):
    mock_client = MagicMock()
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 5, "goal": "First try", "status": "running"},
        "tasks": [],
    }
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 2, "message_id": 5, "goal": "Second try", "status": "done"},
        "tasks": [
            {"id": 7, "plan_id": 2, "type": "msg", "detail": "respond", "status": "done",
             "output": "Ok"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 5, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "Plan: First try" in out
    assert "Replan: Second try" in out


def test_poll_status_exits_on_failed_plan(capsys, plain_caps):
    """Plan with status=failed should exit the poll loop and show the error."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 42, "goal": "Failed", "status": "failed"},
        "tasks": [
            {"id": 5, "plan_id": 1, "type": "msg", "detail": "error", "status": "done",
             "output": "Planning failed: API key not set"},
        ],
        "worker_running": False,
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 5
    out = capsys.readouterr().out
    assert "Planning failed" in out or "API key" in out


def test_poll_waits_on_failed_plan_while_worker_running(capsys, plain_caps):
    """Don't exit on failed plan if worker is still running (replan in progress)."""
    mock_client = MagicMock()

    # First poll: plan failed but worker still running (replan generating)
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 42, "goal": "First try", "status": "failed"},
        "tasks": [],
        "worker_running": True,
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: new plan created by replan, now done
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 2, "message_id": 42, "goal": "Second try", "status": "done"},
        "tasks": [
            {"id": 7, "plan_id": 2, "type": "msg", "detail": "respond", "status": "done",
             "output": "Replanned successfully"},
        ],
        "worker_running": False,
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 7
    out = capsys.readouterr().out
    assert "Replanned successfully" in out
    # Should have polled at least twice (didn't exit on first failed plan)
    assert mock_client.get.call_count >= 2


def test_poll_exits_on_failed_plan_when_worker_idle(capsys, plain_caps):
    """Exit on failed plan when worker is not running (no replan coming)."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 42, "goal": "Failed", "status": "failed"},
        "tasks": [
            {"id": 5, "plan_id": 1, "type": "msg", "detail": "error", "status": "done",
             "output": "Something went wrong"},
        ],
        "worker_running": False,
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 5
    # Should exit after first poll (worker idle, no replan coming)
    assert mock_client.get.call_count == 1


def test_poll_exits_on_done_plan_immediately(capsys, plain_caps):
    """Plan with status=done exits immediately regardless of worker state."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 42, "goal": "Success", "status": "done"},
        "tasks": [
            {"id": 5, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "All good"},
        ],
        "worker_running": True,  # worker still running, but plan is done
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 5
    # Should exit after first poll (plan done, exits immediately)
    assert mock_client.get.call_count == 1
    out = capsys.readouterr().out
    assert "All good" in out


def test_poll_status_exits_when_worker_stopped(capsys, plain_caps):
    """If worker stops and no plan exists, poll should exit with error."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": None,
        "tasks": [],
        "worker_running": False,
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 99, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "worker stopped" in out


def test_poll_status_ignores_plan_from_other_message(capsys, plain_caps):
    """A plan belonging to a different message_id should not be displayed."""
    mock_client = MagicMock()
    # Plan belongs to message 10, but we're polling for message 42
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Old plan", "status": "done"},
        "tasks": [],
        "worker_running": False,
    }
    resp1.raise_for_status = MagicMock()
    mock_client.get.return_value = resp1

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 42, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # The old plan should NOT be shown
    assert "Old plan" not in out


def test_poll_status_quiet_mode_only_shows_done_msg(capsys, plain_caps):
    """In quiet mode, only msg tasks with status='done' are printed."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 7, "goal": "Run ls", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "ls -la", "status": "done",
             "output": "file1.txt"},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Here are your files."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 7, 0, quiet=True, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # msg output shown
    assert "Here are your files." in out
    # exec task NOT shown (quiet mode)
    assert "exec: ls -la" not in out
    assert "file1.txt" not in out
    # Plan header NOT shown (quiet mode)
    assert "Plan:" not in out


def test_poll_status_quiet_mode_skips_running_msg(capsys, plain_caps):
    """In quiet mode, running msg tasks are not printed."""
    mock_client = MagicMock()
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 7, "goal": "Go", "status": "running"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond", "status": "running",
             "output": ""},
        ],
    }
    resp1.raise_for_status = MagicMock()
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 7, "goal": "Go", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done!"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 7, 0, quiet=True, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "Done!" in out


def test_poll_status_shows_output_for_failed_task(capsys, plain_caps):
    """Failed tasks should have their output displayed."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 5, "goal": "Run cmd", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "bad-cmd", "status": "failed",
             "output": "command not found: bad-cmd"},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "The command failed."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 5, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "exec: bad-cmd" in out
    assert "command not found" in out


def test_poll_status_shows_stderr_for_failed_task(capsys, plain_caps):
    """Failed tasks with no output should fall back to stderr."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 5, "goal": "Run cmd", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "ls /nope",
             "status": "failed", "output": "", "stderr": "No such file or directory"},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "It failed."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 5, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "No such file or directory" in out
    assert "exec: ls /nope" in out


def test_poll_status_blank_line_between_tasks(capsys, plain_caps):
    """Non-msg tasks should have a blank line between them."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 5, "goal": "Two cmds", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "ls", "status": "done",
             "output": "file.txt"},
            {"id": 2, "plan_id": 1, "type": "exec", "detail": "pwd", "status": "done",
             "output": "/home"},
            {"id": 3, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 5, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # There should be a blank line between the two exec tasks
    lines = out.splitlines()
    # Find the line with "exec: pwd" and check there's a blank line before it
    pwd_idx = next(i for i, l in enumerate(lines) if "exec: pwd" in l)
    assert lines[pwd_idx - 1].strip() == "", f"Expected blank line before 'exec: pwd', got: {lines[pwd_idx - 1]!r}"


def test_poll_status_msg_task_not_shown_while_running(capsys, plain_caps):
    """Msg tasks should NOT show a header when running, only render_msg_output when done."""
    mock_client = MagicMock()
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 3, "goal": "Hi", "status": "running"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond", "status": "running",
             "output": ""},
        ],
    }
    resp1.raise_for_status = MagicMock()
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 3, "goal": "Hi", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Hello!"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 3, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # msg task header should not appear
    assert "msg: respond" not in out
    # But the actual message output should
    assert "Hello!" in out


def test_poll_status_shows_review_verdict(capsys, plain_caps):
    """Tasks with review_verdict should have the review rendered."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 8, "goal": "Do stuff", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "ls", "status": "done",
             "output": "ok", "review_verdict": "ok"},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 8, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "review: ok" in out


def test_poll_status_timeout_exit(capsys, plain_caps):
    """Poll should exit with timeout error after _MAX_POLL_SECONDS."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 1, "goal": "Slow", "status": "running"},
        "tasks": [],
        "worker_running": True,
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        # Simulate time advancing past 300 seconds on 3rd poll
        if call_count >= 3:
            return 1000.0 + 301
        return 1000.0

    with patch("time.sleep"), patch("time.time", side_effect=fake_time):
        _poll_status(mock_client, "sess", 1, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "timed out" in out


def test_poll_status_worker_grace_period(capsys, plain_caps):
    """Worker stopped should wait 3 polls before giving up."""
    mock_client = MagicMock()

    # Worker stopped, no matching plan — but should wait 3 polls
    resp_no_worker = MagicMock()
    resp_no_worker.json.return_value = {
        "plan": None,
        "tasks": [],
        "worker_running": False,
    }
    resp_no_worker.raise_for_status = MagicMock()

    # Return same response 3 times (triggers exit on 3rd consecutive poll)
    mock_client.get.return_value = resp_no_worker

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 99, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "worker stopped" in out
    # At least 3 GET calls (one per poll cycle that checks)
    assert mock_client.get.call_count >= 3


def test_poll_status_shows_planner_spinner(capsys):
    """While plan is running with no tasks yet, show Planning... spinner."""
    tty_caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=True)
    mock_client = MagicMock()

    # First poll: plan running, no tasks (planner phase)
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Do stuff", "status": "running"},
        "tasks": [],
        "worker_running": True,
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: plan done with a msg task
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Do stuff", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done."},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=tty_caps)

    out = capsys.readouterr().out
    assert "Planning..." in out


def test_poll_status_spinner_emits_newline_when_not_at_col0(capsys):
    """M44b: spinner must emit \\n before first frame when cursor is not at column 0."""
    tty_caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=True)
    mock_client = MagicMock()

    # First poll: plan + running task (triggers spinner)
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Work", "status": "running"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "echo ok",
             "status": "running", "output": None},
        ],
        "worker_running": True,
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: done
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Work", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "echo ok",
             "status": "done", "output": "ok"},
        ],
        "worker_running": False,
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False,
                     caps=tty_caps, _at_col0=False)

    raw = capsys.readouterr().out
    # When _at_col0=False, the spinner emits an extra \n before the first \r frame,
    # producing \n\n\r (task-header-newline + our extra newline + spinner \r).
    assert "\n\n\r" in raw, (
        f"Expected double-newline before first spinner frame (\\n\\n\\r). Got: {raw!r}"
    )


def test_poll_status_spinner_no_extra_newline_when_at_col0(capsys):
    """M44b: spinner must NOT emit an extra \\n when cursor is already at column 0."""
    tty_caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=True)
    mock_client = MagicMock()

    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Work", "status": "running"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "echo ok",
             "status": "running", "output": None},
        ],
        "worker_running": True,
    }
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Work", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "echo ok",
             "status": "done", "output": "ok"},
        ],
        "worker_running": False,
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        # _at_col0=True (default) — cursor already at column 0
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False,
                     caps=tty_caps, _at_col0=True)

    raw = capsys.readouterr().out
    # With _at_col0=True, no extra \n is emitted — the task-header \n + spinner \r
    # gives \n\r, but NOT \n\n\r.
    assert "\r" in raw, "Expected spinner frame \\r in output"
    assert "\n\n\r" not in raw, (
        f"Unexpected double-newline before spinner frame. Got: {raw!r}"
    )


def test_poll_status_spinner_extra_newline_emitted_only_once(capsys):
    """M44g: the extra \\n (for _at_col0=False) is emitted only on the FIRST spinner frame.

    Subsequent frames use \\r to overwrite without adding new lines, so \\n\\n\\r
    appears exactly once regardless of how many spinner frames are rendered.
    """
    tty_caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=True)
    mock_client = MagicMock()

    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Work", "status": "running"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "echo ok",
             "status": "running", "output": None},
        ],
        "worker_running": True,
    }
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Work", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "echo ok",
             "status": "done", "output": "ok"},
        ],
        "worker_running": False,
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False,
                     caps=tty_caps, _at_col0=False)

    raw = capsys.readouterr().out
    # The extra \n appears exactly once (before the first spinner frame), not on
    # subsequent \r-overwrite frames.
    assert raw.count("\n\n\r") == 1, (
        f"Expected \\n\\n\\r exactly once, got {raw.count(chr(10)+chr(10)+chr(13))}. "
        f"Raw: {raw!r}"
    )


def test_chat_http_status_error_on_msg_post(capsys):
    """HTTPStatusError on /msg POST should print status code and continue."""
    from cli import _chat

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Client error", request=MagicMock(), response=mock_response,
    )
    mock_client.post.return_value = mock_response

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "401" in out


def test_chat_empty_input_continues(capsys):
    """Empty or whitespace input should not trigger a POST."""
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["", "   ", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    # No POST should have been made
    mock_client.post.assert_not_called()


def test_chat_user_flag_overrides_getuser(capsys):
    """--user flag should be used as username instead of getpass.getuser()."""
    from cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"queued": True, "message_id": 1, "session": "s"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="myhost"),
        patch("cli._poll_status", return_value=0),
    ):
        _chat(_make_args(user="bob"))

    post_call = mock_client.post.call_args
    assert post_call[1]["json"]["user"] == "bob"
    assert post_call[1]["json"]["session"] == "myhost@bob"


# ── slash commands ────────────────────────────────────────────


def test_slash_exit_breaks_repl(capsys):
    """'/exit' exits the REPL without sending a POST."""
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["/exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    mock_client.post.assert_not_called()
    mock_client.close.assert_called_once()


def test_slash_quit_is_unknown(capsys):
    """'/quit' is not a recognized command."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    _handle_slash("/quit", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "Unknown command" in out


def test_slash_help_prints_commands(capsys):
    """'/help' prints the available slash commands."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    _handle_slash("/help", client, "sess", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "/exit" in out
    assert "/status" in out
    assert "/sessions" in out
    assert "/help" in out
    assert "/clear" in out


def test_slash_status_shows_health(capsys):
    """'/status' shows health, session info, and worker status."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    health_resp = MagicMock()
    health_resp.json.return_value = {"status": "ok"}
    health_resp.raise_for_status = MagicMock()

    info_resp = MagicMock()
    info_resp.json.return_value = {"session": "s", "message_count": 5, "summary": None}
    info_resp.raise_for_status = MagicMock()

    status_resp = MagicMock()
    status_resp.json.return_value = {"worker_running": True, "queue_length": 2}
    status_resp.raise_for_status = MagicMock()

    def get_side(url, **kwargs):
        if url == "/health":
            return health_resp
        if "/info" in url:
            return info_resp
        return status_resp

    client.get.side_effect = get_side

    _handle_slash("/status", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "Health: ok" in out
    assert "Messages: 5" in out
    assert "Worker: running" in out
    assert "Queue: 2" in out


def test_slash_sessions_shows_list(capsys):
    """'/sessions' lists sessions from the server."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    resp = MagicMock()
    resp.json.return_value = [
        {"session": "host@alice", "connector": "cli", "updated_at": None},
    ]
    resp.raise_for_status = MagicMock()
    client.get.return_value = resp

    _handle_slash("/sessions", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "host@alice" in out
    assert "connector: cli" in out


def test_slash_unknown_prints_error(capsys):
    """Unknown slash commands print an error message."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    _handle_slash("/foo", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "Unknown command: /foo" in out
    assert "/help" in out


def test_slash_clear_on_tty(capsys):
    """'/clear' sends escape sequence on TTY."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=True)
    client = MagicMock()

    _handle_slash("/clear", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "\033[2J" in out
    assert "\033[H" in out


def test_old_exit_still_works(capsys):
    """Plain 'exit' text still exits the REPL."""
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    mock_client.post.assert_not_called()
    mock_client.close.assert_called_once()


def test_old_quit_still_works(capsys):
    """Plain 'quit' text also exits the REPL."""
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["quit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    mock_client.post.assert_not_called()
    mock_client.close.assert_called_once()


def test_slash_command_not_sent_to_llm(capsys):
    """Slash commands should never be sent as POST /msg."""
    from cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("cli.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["/unknown", "/help", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    mock_client.post.assert_not_called()


def test_slash_status_unreachable(capsys):
    """'/status' gracefully handles unreachable server."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("refused")

    _handle_slash("/status", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "unreachable" in out
    assert "info unavailable" in out
    assert "Worker: unknown" in out


def test_slash_sessions_empty_list(capsys):
    """'/sessions' with no sessions prints a message."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    resp = MagicMock()
    resp.json.return_value = []
    resp.raise_for_status = MagicMock()
    client.get.return_value = resp

    _handle_slash("/sessions", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "No sessions found" in out


def test_slash_sessions_connect_error(capsys):
    """'/sessions' gracefully handles connection errors."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("refused")

    _handle_slash("/sessions", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "cannot connect" in out


def test_slash_clear_noop_without_tty(capsys):
    """'/clear' does nothing when not on a TTY."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    _handle_slash("/clear", client, "s", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "\033[2J" not in out


# ── readline tab-completion ──────────────────────────────────


def test_setup_readline_registers_completer():
    """_setup_readline should register a completer with readline."""
    import readline

    old = readline.get_completer()
    try:
        _setup_readline()
        completer = readline.get_completer()
        assert completer is not None

        # Tab on "/" should suggest all commands
        results = []
        for i in range(10):
            r = completer("/", i)
            if r is None:
                break
            results.append(r)
        assert set(results) == set(_SLASH_COMMANDS)

        # Tab on "/he" should only suggest "/help"
        assert completer("/he", 0) == "/help"
        assert completer("/he", 1) is None

        # Non-slash text returns nothing
        assert completer("hello", 0) is None
    finally:
        readline.set_completer(old)


# ── poll_status plan_id filtering ────────────────────────────


def test_poll_status_ignores_tasks_from_old_plan(capsys, plain_caps):
    """Tasks from a previous plan should not be displayed."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 2, "message_id": 50, "goal": "New goal", "status": "done"},
        "tasks": [
            # Old task from plan_id=1 — should be ignored
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls -la $HOME",
             "status": "done", "output": "old stuff"},
            # Current task from plan_id=2 — should be shown
            {"id": 7, "plan_id": 2, "type": "msg", "detail": "respond",
             "status": "done", "output": "New answer"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 50, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "New answer" in out
    assert "old stuff" not in out
    assert "ls -la" not in out
    assert result == 7


def test_poll_status_task_count_reflects_current_plan(capsys, plain_caps):
    """Plan header task count should only count tasks from current plan."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 5, "message_id": 20, "goal": "Do it", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 4, "type": "exec", "detail": "old",
             "status": "done", "output": "x"},
            {"id": 2, "plan_id": 4, "type": "msg", "detail": "old msg",
             "status": "done", "output": "x"},
            {"id": 3, "plan_id": 5, "type": "exec", "detail": "new cmd",
             "status": "done", "output": "y"},
            {"id": 4, "plan_id": 5, "type": "msg", "detail": "respond",
             "status": "done", "output": "Done!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 20, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # Should show "2 tasks" (from plan_id=5), not "4 tasks"
    assert "2 tasks" in out
    assert "Done!" in out
    assert "old" not in out.lower().split("do it")[0]  # "old" shouldn't appear before plan header


# ── msg subcommand parsing ───────────────────────────────────


def test_msg_subcommand_parsed():
    parser = build_parser()
    args = parser.parse_args(["msg", "hello world"])
    assert args.command == "msg"
    assert args.message == "hello world"


# ── msg command routes to _msg_cmd ────────────────────────────


def test_msg_command_routing():
    with patch("cli._msg_cmd") as mock_msg:
        with patch("sys.argv", ["kiso", "msg", "test message"]):
            main()
        mock_msg.assert_called_once()


# ── _poll_status shows plan detail ───────────────────────────


def test_poll_status_shows_plan_detail(capsys, plain_caps):
    """Plan detail list should be shown when a plan first appears."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Check stuff", "status": "done",
                 "total_input_tokens": 0, "total_output_tokens": 0, "model": None},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "list files",
             "status": "done", "output": "a.txt", "command": None},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "tell the user",
             "status": "done", "output": "Here are the files"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "[exec]" in out
    assert "[msg]" in out
    assert "list files" in out
    assert "tell the user" in out


# ── _poll_status shows translated command ─────────────────────


def test_poll_status_shows_command(capsys, plain_caps):
    """Translated command should be shown after the task header."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "List", "status": "done",
                 "total_input_tokens": 0, "total_output_tokens": 0, "model": None},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "exec", "detail": "list files",
             "status": "done", "output": "a.txt", "command": "ls -la"},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Done!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "$ ls -la" in out


# ── _poll_status shows token usage ────────────────────────────


def test_poll_status_shows_usage(capsys, plain_caps):
    """Token usage should be shown when plan is done."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Test", "status": "done",
                 "total_input_tokens": 1234, "total_output_tokens": 567,
                 "model": "gpt-4"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Hello!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "1,234" in out
    assert "567" in out
    assert "gpt-4" in out


# ── _poll_status quiet mode hides usage ───────────────────────


def test_poll_status_quiet_hides_usage(capsys, plain_caps):
    """Token usage should not be shown in quiet mode."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Test", "status": "done",
                 "total_input_tokens": 1234, "total_output_tokens": 567,
                 "model": "gpt-4"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Hello!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=True, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "1,234" not in out


# ── verbose mode ──────────────────────────────────────────────


def test_verbose_on_sets_flag(capsys):
    """'/verbose-on' sets _verbose_mode to True."""
    import cli as cli_mod

    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    old = cli_mod._verbose_mode
    try:
        cli_mod._verbose_mode = False
        _handle_slash("/verbose-on", client, "s", "alice", caps, "Bot")
        assert cli_mod._verbose_mode is True
        out = capsys.readouterr().out
        assert "ON" in out
    finally:
        cli_mod._verbose_mode = old


def test_verbose_off_sets_flag(capsys):
    """'/verbose-off' sets _verbose_mode to False."""
    import cli as cli_mod

    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    old = cli_mod._verbose_mode
    try:
        cli_mod._verbose_mode = True
        _handle_slash("/verbose-off", client, "s", "alice", caps, "Bot")
        assert cli_mod._verbose_mode is False
        out = capsys.readouterr().out
        assert "OFF" in out
    finally:
        cli_mod._verbose_mode = old


def test_help_includes_verbose_commands(capsys):
    """'/help' output includes /verbose-on and /verbose-off."""
    caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=False)
    client = MagicMock()

    _handle_slash("/help", client, "sess", "alice", caps, "Bot")

    out = capsys.readouterr().out
    assert "/verbose-on" in out
    assert "/verbose-off" in out


def test_poll_status_accepts_verbose_parameter(capsys, plain_caps):
    """_poll_status accepts verbose parameter without error."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Test", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Hello!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=True, caps=plain_caps)

    assert result == 1


def test_poll_status_passes_verbose_query_param(capsys, plain_caps):
    """When verbose=True, /status is called with verbose=true query param."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Test", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Ok"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=True, caps=plain_caps)

    # Check that GET /status was called with verbose=true
    get_call = mock_client.get.call_args
    params = get_call.kwargs.get("params") or get_call[1].get("params", {})
    assert params.get("verbose") == "true"


def test_poll_status_verbose_false_query_param(capsys, plain_caps):
    """When verbose=False, /status is called with verbose=false query param."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Test", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Ok"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    get_call = mock_client.get.call_args
    params = get_call.kwargs.get("params") or get_call[1].get("params", {})
    assert params.get("verbose") == "false"


def test_slash_commands_include_verbose():
    """_SLASH_COMMANDS list includes verbose commands for tab completion."""
    assert "/verbose-on" in _SLASH_COMMANDS
    assert "/verbose-off" in _SLASH_COMMANDS


# ── Persistent readline history ──────────────────────────────


class TestReadlineHistory:
    def test_setup_readline_loads_history(self, tmp_path):
        """_setup_readline calls readline.read_history_file with correct path."""
        mock_rl = MagicMock()
        mock_rl.read_history_file.side_effect = FileNotFoundError()
        with patch.dict("sys.modules", {"readline": mock_rl}), \
             patch("kiso.config.KISO_DIR", tmp_path):
            _setup_readline()
        mock_rl.read_history_file.assert_called_once_with(str(tmp_path / ".chat_history"))

    def test_setup_readline_handles_missing_history(self):
        """FileNotFoundError from read_history_file is silently caught."""
        mock_rl = MagicMock()
        mock_rl.read_history_file.side_effect = FileNotFoundError()
        with patch.dict("sys.modules", {"readline": mock_rl}), \
             patch("kiso.config.KISO_DIR", MagicMock()):
            _setup_readline()
        # Should not raise — FileNotFoundError is caught
        mock_rl.read_history_file.assert_called_once()

    def test_setup_readline_sets_history_length(self):
        """readline.set_history_length(500) is called."""
        mock_rl = MagicMock()
        with patch.dict("sys.modules", {"readline": mock_rl}), \
             patch("kiso.config.KISO_DIR", MagicMock()):
            _setup_readline()
        mock_rl.set_history_length.assert_called_once_with(500)

    def test_save_readline_history_writes_file(self, tmp_path):
        """_save_readline_history calls readline.write_history_file."""
        mock_rl = MagicMock()
        with patch.dict("sys.modules", {"readline": mock_rl}), \
             patch("kiso.config.KISO_DIR", tmp_path):
            _save_readline_history()
        mock_rl.write_history_file.assert_called_once_with(str(tmp_path / ".chat_history"))

    def test_save_readline_history_handles_write_error(self, tmp_path):
        """OSError from write_history_file is silently caught."""
        mock_rl = MagicMock()
        mock_rl.write_history_file.side_effect = OSError("disk full")
        with patch.dict("sys.modules", {"readline": mock_rl}), \
             patch("kiso.config.KISO_DIR", tmp_path):
            _save_readline_history()
        # Should not raise

    def test_save_readline_history_no_readline(self):
        """_save_readline_history handles ImportError gracefully."""
        with patch.dict("sys.modules", {"readline": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                _save_readline_history()
        # Should not raise


# ── seen dict tracking in _poll_status ────────────────────────


def test_poll_status_skips_unchanged_task(capsys, plain_caps):
    """Two polls returning the same task with identical status/review/substatus/llm_calls
    should only render the task header once (not duplicated)."""
    mock_client = MagicMock()

    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "running"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "running",
             "output": "", "llm_calls": "[]"},
        ],
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: identical task (same status, no review, same substatus, same llm count)
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "done"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "running",
             "output": "", "llm_calls": "[]"},
            {"id": 4, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done.", "llm_calls": "[]"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # The task header "exec: ls" should appear exactly once (not re-rendered on second poll)
    assert out.count("exec: ls") == 1
    assert "Done." in out


def test_poll_status_rerenders_on_substatus_change(capsys, plain_caps):
    """When substatus changes (translating -> executing) but status stays 'running',
    the task_key changes so the code reaches the prev_status == status branch
    which renders just the review line (not the full header again)."""
    mock_client = MagicMock()

    # First poll: task running with substatus "translating"
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "running"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "running",
             "output": "", "substatus": "translating", "llm_calls": "[]"},
        ],
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: same task, substatus changed to "executing", still running
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "running"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "running",
             "output": "", "substatus": "executing", "llm_calls": "[]"},
        ],
    }
    resp2.raise_for_status = MagicMock()

    # Third poll: done
    resp3 = MagicMock()
    resp3.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "done"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "ls", "status": "done",
             "output": "file.txt", "substatus": "", "llm_calls": "[]"},
            {"id": 4, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Ok.", "llm_calls": "[]"},
        ],
    }
    resp3.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2, resp3]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # First render: prev_status is None so full header is shown
    assert "exec: ls" in out
    # The task_key changed (substatus "translating" -> "executing") so the task is
    # processed again.  Since prev_status == status == "running", the code enters the
    # "show just review" branch (lines 532-541).  With no review_verdict the review
    # line is empty, so nothing extra is printed — but the task was NOT skipped.
    # The header appears exactly twice: once for the initial running render (prev None→running)
    # and once for the done render (status running→done).  The substatus change does NOT
    # produce a third header — it only enters the review branch.
    assert out.count("exec: ls") == 2
    # Final done render shows output
    assert "file.txt" in out


def test_poll_status_renders_search_task(capsys, plain_caps):
    """A search task should render like an exec task: header + output + review."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Find it", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "search", "detail": "find config files",
             "status": "done", "output": "config.toml\nconfig.yaml",
             "review_verdict": "ok", "llm_calls": "[]"},
            {"id": 2, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Found them.", "llm_calls": "[]"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    assert result == 2
    out = capsys.readouterr().out
    # Search task header rendered (type label is "search")
    assert "search: find config files" in out
    # Search task output rendered
    assert "config.toml" in out
    # Review rendered
    assert "review: ok" in out
    # Msg output rendered
    assert "Found them." in out


def test_poll_status_shows_review_on_llm_count_change(capsys, plain_caps):
    """When llm_call_count changes but status stays the same, only the review line
    is rendered (not the full task header again)."""
    mock_client = MagicMock()

    # First poll: task running with 0 llm_calls, no review
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "running"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "compile", "status": "running",
             "output": "", "llm_calls": "[]"},
        ],
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: still running, but llm_call_count went from 0 → 1, review_verdict added
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "running"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "compile", "status": "running",
             "output": "", "review_verdict": "ok",
             "llm_calls": '[{"role":"translator","model":"m","input_tokens":10,"output_tokens":5}]'},
        ],
    }
    resp2.raise_for_status = MagicMock()

    # Third poll: task done + plan done
    resp3 = MagicMock()
    resp3.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "done"},
        "tasks": [
            {"id": 3, "plan_id": 1, "type": "exec", "detail": "compile", "status": "done",
             "output": "success", "review_verdict": "ok",
             "llm_calls": '[{"role":"translator","model":"m","input_tokens":10,"output_tokens":5}]'},
            {"id": 4, "plan_id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Built.", "llm_calls": "[]"},
        ],
    }
    resp3.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2, resp3]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=plain_caps)

    out = capsys.readouterr().out
    # Full header appears twice: once for initial running render (prev None→running)
    # and once for done render (status running→done).  The llm_count change on the
    # second poll does NOT produce a third header — it only shows the review line.
    assert out.count("exec: compile") == 2
    # Review line appears from the llm_count change (second poll hits prev_status == status branch)
    assert "review: ok" in out
    # Final done render shows output
    assert "success" in out


# ── M41: CLI polling UX gaps ───────────────────────────────────────────────────


def test_m41_poll_every_is_160ms():
    """M41: poll interval must be 2 iterations × 80 ms = 160 ms (was 480 ms)."""
    assert _POLL_EVERY == 2


def test_m41_shows_spinner_before_plan_created(capsys):
    """M41: planning spinner must activate when worker is running but plan not yet created.

    During the pre-plan phase (classifier + planner LLM calls, typically 4-15 s) the
    server returns worker_running=True but plan=None.  The CLI must show the
    'Planning...' spinner rather than appearing frozen.
    """
    tty_caps = TermCaps(color=False, unicode=False, width=80, height=24, tty=True)
    mock_client = MagicMock()

    # First poll: worker running, no plan yet (classifier + planner not done)
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": None,
        "tasks": [],
        "worker_running": True,
    }
    resp1.raise_for_status = MagicMock()

    # Second poll: everything done
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Do stuff", "status": "done"},
        "tasks": [
            {"id": 1, "plan_id": 1, "type": "msg", "detail": "respond",
             "status": "done", "output": "Done."},
        ],
        "worker_running": False,
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 10, 0, quiet=False, verbose=False, caps=tty_caps)

    out = capsys.readouterr().out
    assert "Planning..." in out



# ---------------------------------------------------------------------------
# M49: versioning — kiso/_version.py + kiso version command
# ---------------------------------------------------------------------------


class TestVersionFile:
    def test_version_file_exists(self):
        """kiso/_version.py must exist and define __version__."""
        from kiso._version import __version__ as v
        assert isinstance(v, str)
        assert len(v) > 0

    def test_version_format_semver(self):
        """Version string must follow semver (MAJOR.MINOR.PATCH)."""
        from kiso._version import __version__ as v
        parts = v.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_cli_exposes_version(self):
        """cli module must re-export __version__ from kiso._version."""
        assert isinstance(__version__, str)
        assert __version__ == __import__("kiso._version", fromlist=["__version__"]).__version__


class TestVersionCommand:
    def test_version_subcommand_exists(self):
        """'kiso version' must be a registered subcommand."""
        parser = build_parser()
        args = parser.parse_args(["version"])
        assert args.command == "version"

    def test_version_command_prints_version(self, capsys):
        """'kiso version' must print 'kiso {version}' to stdout."""
        from kiso._version import __version__ as v
        with patch("sys.argv", ["kiso", "version"]):
            main()
        out = capsys.readouterr().out
        assert f"kiso {v}" in out

    def test_version_flag_short_exits_zero(self, capsys):
        """'kiso -V' must exit cleanly."""
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["-V"])
        assert exc.value.code == 0

    def test_version_flag_long_prints_version(self, capsys):
        """'kiso --version' must print 'kiso {version}' and exit."""
        from kiso._version import __version__ as v
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        out = capsys.readouterr().out
        assert f"kiso {v}" in out
        assert exc.value.code == 0

    def test_help_description_includes_version(self, capsys):
        """'kiso --help' description must mention the current version."""
        from kiso._version import __version__ as v
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--help"])
        out = capsys.readouterr().out
        assert v in out
