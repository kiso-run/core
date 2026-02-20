"""Tests for kiso.cli — argument parsing, subcommand routing, and chat REPL."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from kiso.cli import _ExitRepl, _handle_slash, _poll_status, build_parser, main
from kiso.render import TermCaps


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


@pytest.mark.parametrize("cmd", ["skill", "connector", "sessions", "env"])
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
        from kiso.cli import _serve

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


# ── _chat REPL ───────────────────────────────────────────────


def _mock_config(has_cli_token: bool = True):
    cfg = MagicMock()
    cfg.tokens = {"cli": "tok-abc"} if has_cli_token else {}
    return cfg


def _make_args(session=None, api="http://localhost:8333", quiet=False, user=None):
    return argparse.Namespace(session=session, api=api, quiet=quiet, user=user, command=None)


def test_chat_exits_on_exit_command(capsys):
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
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
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=KeyboardInterrupt),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="host1"),
    ):
        _chat(_make_args())

    mock_client.close.assert_called_once()


def test_chat_exits_on_eof(capsys):
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=EOFError),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="host1"),
    ):
        _chat(_make_args())

    mock_client.close.assert_called_once()


def test_chat_missing_cli_token(capsys):
    from kiso.cli import _chat

    with (
        patch("kiso.config.load_config", return_value=_mock_config(has_cli_token=False)),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        pytest.raises(SystemExit, match="1"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "no 'cli' token" in out


def test_chat_default_session():
    from kiso.cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"queued": True, "message_id": 1, "session": "s"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="bob"),
        patch("socket.gethostname", return_value="myhost"),
        patch("kiso.cli._poll_status", return_value=0),
    ):
        _chat(_make_args(session=None))

    # Check the session passed to POST /msg
    post_call = mock_client.post.call_args
    assert post_call[1]["json"]["session"] == "myhost@bob"


def test_chat_connection_error(capsys):
    from kiso.cli import _chat

    mock_client = MagicMock()
    mock_client.post.side_effect = [
        httpx.ConnectError("refused"),
        None,  # won't be reached due to "exit"
    ]

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "cannot connect" in out


def test_chat_untrusted_message(capsys):
    from kiso.cli import _chat

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
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    out = capsys.readouterr().out
    assert "not trusted" in out


def test_chat_cancel_on_ctrl_c_during_poll(capsys):
    from kiso.cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"queued": True, "message_id": 1, "session": "s"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
        patch("kiso.cli._poll_status", side_effect=KeyboardInterrupt),
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
            {"id": 5, "type": "msg", "detail": "respond", "status": "done",
             "output": "Hello!"},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, caps=plain_caps)

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
            {"id": 3, "type": "exec", "detail": "ls", "status": "running", "output": ""},
        ],
    }
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 10, "goal": "Run it", "status": "done"},
        "tasks": [
            {"id": 3, "type": "exec", "detail": "ls", "status": "done",
             "output": "file.txt"},
            {"id": 4, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done."},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 10, 0, quiet=False, caps=plain_caps)

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
            {"id": 7, "type": "msg", "detail": "respond", "status": "done",
             "output": "Ok"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 5, 0, quiet=False, caps=plain_caps)

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
            {"id": 5, "type": "msg", "detail": "error", "status": "done",
             "output": "Planning failed: API key not set"},
        ],
        "worker_running": False,
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        result = _poll_status(mock_client, "sess", 42, 0, quiet=False, caps=plain_caps)

    assert result == 5
    out = capsys.readouterr().out
    assert "Planning failed" in out or "API key" in out


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
        _poll_status(mock_client, "sess", 99, 0, quiet=False, caps=plain_caps)

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
        _poll_status(mock_client, "sess", 42, 0, quiet=False, caps=plain_caps)

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
            {"id": 1, "type": "exec", "detail": "ls -la", "status": "done",
             "output": "file1.txt"},
            {"id": 2, "type": "msg", "detail": "respond", "status": "done",
             "output": "Here are your files."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 7, 0, quiet=True, caps=plain_caps)

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
            {"id": 1, "type": "msg", "detail": "respond", "status": "running",
             "output": ""},
        ],
    }
    resp1.raise_for_status = MagicMock()
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 7, "goal": "Go", "status": "done"},
        "tasks": [
            {"id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done!"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 7, 0, quiet=True, caps=plain_caps)

    out = capsys.readouterr().out
    assert "Done!" in out


def test_poll_status_shows_output_for_failed_task(capsys, plain_caps):
    """Failed tasks should have their output displayed."""
    mock_client = MagicMock()
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "plan": {"id": 1, "message_id": 5, "goal": "Run cmd", "status": "done"},
        "tasks": [
            {"id": 1, "type": "exec", "detail": "bad-cmd", "status": "failed",
             "output": "command not found: bad-cmd"},
            {"id": 2, "type": "msg", "detail": "respond", "status": "done",
             "output": "The command failed."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 5, 0, quiet=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "exec: bad-cmd" in out
    assert "command not found" in out


def test_poll_status_msg_task_not_shown_while_running(capsys, plain_caps):
    """Msg tasks should NOT show a header when running, only render_msg_output when done."""
    mock_client = MagicMock()
    resp1 = MagicMock()
    resp1.json.return_value = {
        "plan": {"id": 1, "message_id": 3, "goal": "Hi", "status": "running"},
        "tasks": [
            {"id": 1, "type": "msg", "detail": "respond", "status": "running",
             "output": ""},
        ],
    }
    resp1.raise_for_status = MagicMock()
    resp2 = MagicMock()
    resp2.json.return_value = {
        "plan": {"id": 1, "message_id": 3, "goal": "Hi", "status": "done"},
        "tasks": [
            {"id": 1, "type": "msg", "detail": "respond", "status": "done",
             "output": "Hello!"},
        ],
    }
    resp2.raise_for_status = MagicMock()
    mock_client.get.side_effect = [resp1, resp2]

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 3, 0, quiet=False, caps=plain_caps)

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
            {"id": 1, "type": "exec", "detail": "ls", "status": "done",
             "output": "ok", "review_verdict": "ok"},
            {"id": 2, "type": "msg", "detail": "respond", "status": "done",
             "output": "Done."},
        ],
    }
    status_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = status_resp

    with patch("time.sleep"):
        _poll_status(mock_client, "sess", 8, 0, quiet=False, caps=plain_caps)

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
        _poll_status(mock_client, "sess", 1, 0, quiet=False, caps=plain_caps)

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
        _poll_status(mock_client, "sess", 99, 0, quiet=False, caps=plain_caps)

    out = capsys.readouterr().out
    assert "worker stopped" in out
    # At least 3 GET calls (one per poll cycle that checks)
    assert mock_client.get.call_count >= 3


def test_chat_http_status_error_on_msg_post(capsys):
    """HTTPStatusError on /msg POST should print status code and continue."""
    from kiso.cli import _chat

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
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
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
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
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
    from kiso.cli import _chat

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"queued": True, "message_id": 1, "session": "s"}
    mock_resp.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_resp

    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["hello", "exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="myhost"),
        patch("kiso.cli._poll_status", return_value=0),
    ):
        _chat(_make_args(user="bob"))

    post_call = mock_client.post.call_args
    assert post_call[1]["json"]["user"] == "bob"
    assert post_call[1]["json"]["session"] == "myhost@bob"


# ── slash commands ────────────────────────────────────────────


def test_slash_exit_breaks_repl(capsys):
    """'/exit' exits the REPL without sending a POST."""
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["/exit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    mock_client.post.assert_not_called()
    mock_client.close.assert_called_once()


def test_slash_quit_breaks_repl(capsys):
    """'/quit' exits the REPL without sending a POST."""
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
        patch("httpx.Client", return_value=mock_client),
        patch("builtins.input", side_effect=["/quit"]),
        patch("getpass.getuser", return_value="alice"),
        patch("socket.gethostname", return_value="h"),
    ):
        _chat(_make_args())

    mock_client.post.assert_not_called()
    mock_client.close.assert_called_once()


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
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
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
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
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
    from kiso.cli import _chat

    mock_client = MagicMock()
    with (
        patch("kiso.config.load_config", return_value=_mock_config()),
        patch("kiso.render.detect_caps", return_value=TermCaps(False, False, 80, 24, False)),
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
