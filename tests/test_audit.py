"""Tests for kiso/audit.py — structured JSONL audit logging."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch

import pytest

from kiso.audit import _audit_dir_ready, _ensure_audit_dir, _write_entry, log_llm_call, log_task, log_review, log_webhook


# --- _write_entry ---


class TestWriteEntry:
    def test_creates_jsonl_file(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "test", "data": "hello"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "test"
        assert entry["data"] == "hello"

    def test_timestamp_iso_format(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "test"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        ts = entry["timestamp"]
        # Should parse as ISO 8601
        dt = datetime.fromisoformat(ts)
        assert dt.year >= 2024

    def test_multiple_entries_append(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "first"})
            _write_entry({"type": "second"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "first"
        assert json.loads(lines[1])["type"] == "second"

    def test_secret_masking_in_entry(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry(
                {"type": "task", "detail": "echo sk-secret-key-123"},
                deploy_secrets={"KEY": "sk-secret-key-123"},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert "sk-secret-key-123" not in entry["detail"]
        assert "[REDACTED]" in entry["detail"]

    def test_session_secret_masking(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry(
                {"type": "task", "detail": "use token tok_abc123"},
                session_secrets={"api_token": "tok_abc123"},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert "tok_abc123" not in entry["detail"]
        assert "[REDACTED]" in entry["detail"]

    def test_no_masking_without_secrets(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "task", "detail": "echo hello"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["detail"] == "echo hello"

    def test_write_failure_does_not_raise(self, tmp_path):
        """Audit writes should never break the app."""
        with patch("kiso.audit.KISO_DIR", tmp_path / "nonexistent" / "deep" / "path"):
            # Make the path unwritable by using a file as parent
            (tmp_path / "nonexistent").write_text("not a dir")
            # Should not raise
            _write_entry({"type": "test"})

    def test_creates_audit_dir_if_needed(self, tmp_path):
        assert not (tmp_path / "audit").exists()
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "test"})
        assert (tmp_path / "audit").is_dir()

    def test_timestamp_not_masked(self, tmp_path):
        """Timestamp field should never be masked even if it matches a secret."""
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry(
                {"type": "test", "value": "2024-01-15"},
                deploy_secrets={"KEY": "2024-01-15"},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        # timestamp should be ISO 8601 from datetime, not masked
        assert "timestamp" in entry
        assert entry["timestamp"].startswith("20")

    def test_non_string_fields_not_masked(self, tmp_path):
        """Non-string fields should pass through unchanged."""
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry(
                {"type": "task", "count": 42, "active": True},
                deploy_secrets={"KEY": "secret"},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["count"] == 42
        assert entry["active"] is True

    def test_caller_dict_not_mutated(self, tmp_path):
        """_write_entry must not mutate the caller's dict."""
        original = {"type": "test", "detail": "echo sk-secret-key-123"}
        snapshot = dict(original)
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry(original, deploy_secrets={"KEY": "sk-secret-key-123"})
        assert original == snapshot  # no timestamp, no redaction

    def test_type_field_not_masked(self, tmp_path):
        """The 'type' field must never be masked even if it matches a secret."""
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry(
                {"type": "task", "detail": "task"},
                deploy_secrets={"KEY": "task"},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["type"] == "task"
        assert "[REDACTED]" in entry["detail"]

    def test_single_datetime_for_timestamp_and_filename(self, tmp_path):
        """Timestamp in entry and filename date must come from the same instant."""
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "test"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        filename_date = files[0].stem  # e.g. "2026-02-18"
        entry = json.loads(files[0].read_text().strip())
        ts_date = entry["timestamp"][:10]  # e.g. "2026-02-18"
        assert ts_date == filename_date

    def test_audit_dir_permissions(self, tmp_path):
        """Audit directory should have 0o700 permissions."""
        import stat
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "test"})
        audit_dir = tmp_path / "audit"
        mode = stat.S_IMODE(audit_dir.stat().st_mode)
        assert mode == 0o700

    def test_audit_file_permissions(self, tmp_path):
        """Audit files should have 0o600 permissions."""
        import stat
        with patch("kiso.audit.KISO_DIR", tmp_path):
            _write_entry({"type": "test"})
        files = list((tmp_path / "audit").glob("*.jsonl"))
        mode = stat.S_IMODE(files[0].stat().st_mode)
        assert mode == 0o600


# --- log_llm_call ---


class TestLogLlmCall:
    def test_structure(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_llm_call("sess1", "planner", "gpt-4", "openrouter", 100, 50, 1200, "ok")

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["type"] == "llm"
        assert entry["session"] == "sess1"
        assert entry["role"] == "planner"
        assert entry["model"] == "gpt-4"
        assert entry["provider"] == "openrouter"
        assert entry["input_tokens"] == 100
        assert entry["output_tokens"] == 50
        assert entry["duration_ms"] == 1200
        assert entry["status"] == "ok"
        assert "timestamp" in entry

    def test_error_status(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_llm_call("sess1", "worker", "gpt-3.5", "openrouter", 0, 0, 50, "error")

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["status"] == "error"
        assert entry["input_tokens"] == 0
        assert entry["output_tokens"] == 0


# --- log_task ---


class TestLogTask:
    def test_structure(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_task("sess1", 42, "exec", "echo hello", "done", 500, 6)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["type"] == "task"
        assert entry["session"] == "sess1"
        assert entry["task_id"] == 42
        assert entry["task_type"] == "exec"
        assert entry["detail"] == "echo hello"
        assert entry["status"] == "done"
        assert entry["duration_ms"] == 500
        assert entry["output_length"] == 6
        assert "timestamp" in entry

    def test_secret_masking_in_detail(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_task(
                "sess1", 42, "exec", "curl -H 'Auth: sk-secret'", "done", 500, 10,
                deploy_secrets={"KEY": "sk-secret"},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert "sk-secret" not in entry["detail"]
        assert "[REDACTED]" in entry["detail"]

    def test_msg_task_type(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_task("sess1", 7, "msg", "generate summary", "done", 300, 42)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["task_type"] == "msg"
        assert entry["detail"] == "generate summary"
        assert entry["output_length"] == 42

    def test_skill_task_type(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_task("sess1", 3, "skill", "search", "done", 1500, 200)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["task_type"] == "skill"


# --- log_review ---


class TestLogReview:
    def test_structure(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_review("sess1", 42, "ok", True)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["type"] == "review"
        assert entry["session"] == "sess1"
        assert entry["task_id"] == 42
        assert entry["verdict"] == "ok"
        assert entry["has_learning"] is True
        assert "timestamp" in entry

    def test_has_learning_false(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_review("sess1", 42, "replan", False)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["verdict"] == "replan"
        assert entry["has_learning"] is False


# --- log_webhook ---


class TestLogWebhook:
    def test_structure(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_webhook("sess1", 42, "https://example.com/hook", 200, 1)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["type"] == "webhook"
        assert entry["session"] == "sess1"
        assert entry["task_id"] == 42
        assert entry["url"] == "https://example.com/hook"
        assert entry["status"] == 200
        assert entry["attempts"] == 1
        assert "timestamp" in entry

    def test_failed_webhook(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_webhook("sess1", 42, "https://example.com/hook", 500, 3)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["status"] == 500
        assert entry["attempts"] == 3

    def test_connection_error_webhook(self, tmp_path):
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_webhook("sess1", 42, "https://example.com/hook", 0, 3)

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["status"] == 0
        assert entry["attempts"] == 3

    def test_webhook_url_secret_masking(self, tmp_path):
        """Webhook URL should be sanitized when secrets are provided."""
        token = "xoxb-super-secret-token"
        url = f"https://hooks.slack.com/services/{token}"
        with patch("kiso.audit.KISO_DIR", tmp_path):
            log_webhook(
                "sess1", 42, url, 200, 1,
                deploy_secrets={"SLACK_TOKEN": token},
            )

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert token not in entry["url"]
        assert "[REDACTED]" in entry["url"]


# --- Concurrent writes ---


class TestConcurrentWrites:
    def test_concurrent_writes_no_corruption(self, tmp_path):
        """Use ThreadPoolExecutor to write 50 entries concurrently, verify all lines are valid JSON."""
        def _write(i: int):
            with patch("kiso.audit.KISO_DIR", tmp_path):
                _write_entry({"type": "concurrent", "index": i})

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(_write, range(50)))

        files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 50
        for line in lines:
            entry = json.loads(line)
            assert entry["type"] == "concurrent"
            assert "index" in entry


# --- Lock release on write error ---


class TestLockRelease:
    def test_lock_released_on_write_error(self, tmp_path):
        """Simulate write error, verify next write succeeds (lock was released)."""
        with patch("kiso.audit.KISO_DIR", tmp_path):
            # First call: force json.dumps to raise after lock is acquired
            with patch("kiso.audit.json.dumps", side_effect=ValueError("boom")):
                _write_entry({"type": "fail"})  # should not raise

            # Second call: should succeed — lock must have been released
            _write_entry({"type": "success"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "success"


# --- _ensure_audit_dir (M61c) ---


class TestEnsureAuditDir:
    def test_creates_dir_and_sets_permissions(self, tmp_path):
        """_ensure_audit_dir creates the directory with 0o700 permissions."""
        import os
        import stat

        audit_dir = tmp_path / "audit"
        guard = set()
        _ensure_audit_dir.__globals__  # touch to confirm it exists
        # Call directly with the guard patched to an empty set
        with patch("kiso.audit._audit_dir_ready", guard):
            _ensure_audit_dir(audit_dir)

        assert audit_dir.is_dir()
        mode = stat.S_IMODE(os.stat(audit_dir).st_mode)
        assert mode == 0o700

    def test_mkdir_called_only_once(self, tmp_path):
        """Multiple _ensure_audit_dir calls only mkdir once."""
        from unittest.mock import call
        from unittest.mock import patch as _patch

        audit_dir = tmp_path / "audit_once"
        guard: set = set()
        mkdir_calls = []

        orig_mkdir = audit_dir.__class__.mkdir

        with patch("kiso.audit._audit_dir_ready", guard), \
             patch("kiso.audit.os.chmod"):
            # First call: dir doesn't exist yet — use real mkdir
            _ensure_audit_dir(audit_dir)
            call_count_after_first = len(guard)

            # Second call: dir already in guard — should be a no-op
            with patch.object(type(audit_dir), "mkdir") as mock_mkdir, \
                 patch("kiso.audit.os.chmod") as mock_chmod:
                _ensure_audit_dir(audit_dir)
                mock_mkdir.assert_not_called()
                mock_chmod.assert_not_called()

        assert call_count_after_first == 1  # added exactly once

    def test_write_entry_only_inits_dir_once(self, tmp_path):
        """N consecutive _write_entry calls trigger mkdir+chmod only once."""
        import kiso.audit as audit_mod

        guard: set = set()
        with patch("kiso.audit.KISO_DIR", tmp_path), \
             patch("kiso.audit._audit_dir_ready", guard), \
             patch("kiso.audit.os.chmod") as mock_chmod:
            for _ in range(5):
                _write_entry({"type": "perf_test"})

        # chmod called exactly once for the audit dir
        assert mock_chmod.call_count == 1
