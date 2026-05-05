"""Tests for the shared `tests/_helpers.py` factory module.

Locks the contract of `make_task_dict()`:
- Default-only call returns the canonical 4-key shape
  (`type`, `detail`, `status`, `output`).
- Kwargs override the defaults (per-key).
- Optional kwargs (`expect`, `args`, `id`) land in the result
  only when explicitly set; default calls do NOT include them.
"""

from __future__ import annotations

from tests._helpers import make_task_dict


class TestMakeTaskDict:
    def test_default_shape(self):
        d = make_task_dict()
        assert d == {
            "type": "exec",
            "detail": "echo hi",
            "status": "done",
            "output": "",
        }

    def test_kwargs_override_defaults(self):
        d = make_task_dict(
            type="msg", detail="report sent",
            status="failed", output="error message",
        )
        assert d["type"] == "msg"
        assert d["detail"] == "report sent"
        assert d["status"] == "failed"
        assert d["output"] == "error message"

    def test_optional_keys_absent_by_default(self):
        d = make_task_dict()
        assert "expect" not in d
        assert "args" not in d
        assert "id" not in d

    def test_expect_lands_only_when_set(self):
        d = make_task_dict(expect="file created")
        assert d["expect"] == "file created"

    def test_args_lands_only_when_set(self):
        d = make_task_dict(args={"flag": True})
        assert d["args"] == {"flag": True}

    def test_id_lands_only_when_set(self):
        d = make_task_dict(id=42)
        assert d["id"] == 42

    def test_none_optional_kwargs_excluded(self):
        d = make_task_dict(expect=None, args=None, id=None)
        assert "expect" not in d
        assert "args" not in d
        assert "id" not in d

    def test_returned_dict_is_independent(self):
        """Each call returns a fresh dict; mutations don't leak."""
        d1 = make_task_dict()
        d2 = make_task_dict()
        d1["output"] = "modified"
        assert d2["output"] == ""
