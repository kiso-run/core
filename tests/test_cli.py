"""Tests for kiso.cli — argument parsing, subcommand routing, and chat REPL."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from kiso.cli import _poll_status, build_parser, main
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


def _make_args(session=None, api="http://localhost:8333", quiet=False):
    return argparse.Namespace(session=session, api=api, quiet=quiet, command=None)


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
    assert "Bot: Hello!" in out


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
    assert "Bot: Done." in out


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
