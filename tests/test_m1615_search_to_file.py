"""M1615 — Search-to-file pipeline invariants.

F7 surfaced a real-world bug where a plan of shape ``[mcp/search,
exec(write to pub/X.md), msg]`` produced a published markdown file
of 0 bytes. Root cause: the worker translator (an LLM) emitted a
file-creation command without embedding the prior MCP output content
(e.g. ``touch pub/X.md`` instead of a heredoc carrying the search
results).

This module pins two invariants that together close that bug class:

1. **Formatter invariant** — ``_format_plan_outputs_for_msg`` must
   surface the *content* of an MCP task's output verbatim (for
   outputs under the budget) so the worker translator has access to
   it. Existing TestFormatPlanOutputsForMsg already covers exec
   outputs; this module adds the MCP variant explicitly.

2. **Worker-prompt invariant** — the worker translator's prompt must
   tell it how to consume prior task output when the user-visible
   detail asks to "write the content into a file": embed the prior
   output via heredoc when it is shown inline, otherwise read it
   from the saved-output file. Without this rule the translator
   defaults to a placeholder file (the F7 0-byte symptom).

3. **Pipeline shape invariant** — given a plan with an MCP task that
   returns content X and an exec task whose translation is a
   heredoc-based file write, after execution the published file
   must be non-empty.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.worker.replan import _format_plan_outputs_for_msg


_WORKER_MD = (
    Path(__file__).resolve().parent.parent / "kiso" / "roles" / "worker.md"
)


# ---------------------------------------------------------------------------
# 1. Formatter — MCP outputs include their content verbatim
# ---------------------------------------------------------------------------


class TestFormatPlanOutputsIncludesMcpContent:
    """The formatter must surface MCP output content so the worker
    translator can consume it when the next exec writes to file."""

    def test_mcp_output_content_is_included_verbatim(self):
        """Small MCP output → the actual content text appears in the
        formatter result, not just a header."""
        mcp_output = (
            "# Top languages 2025\n"
            "1. Python — data science / AI\n"
            "2. JavaScript — web frontend\n"
            "3. TypeScript — typed JS for large codebases\n"
            "4. Java — enterprise / Android\n"
            "5. Go — cloud / backend\n"
        )
        outputs = [
            {
                "index": 1, "type": "mcp",
                "detail": "search top programming languages 2025",
                "output": mcp_output, "status": "done",
            },
        ]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        # Header is present.
        assert "[1] mcp:" in result
        # The full content is present verbatim.
        for token in ("Python", "JavaScript", "TypeScript", "Java", "Go"):
            assert token in result, (
                f"MCP output content {token!r} missing from formatter "
                f"result; the worker translator will receive only the "
                f"header and emit a placeholder file."
            )

    def test_mcp_output_status_done_is_marked(self):
        """The status marker is required so the translator knows the
        prior task succeeded and its content is reusable."""
        outputs = [{
            "index": 1, "type": "mcp",
            "detail": "search X",
            "output": "result A\nresult B",
            "status": "done",
        }]
        result = _format_plan_outputs_for_msg(outputs, budget=8000)
        assert "Status: done" in result


# ---------------------------------------------------------------------------
# 2. Worker prompt — heredoc rule for "write prior output to file"
# ---------------------------------------------------------------------------


class TestWorkerPromptWritePriorOutputToFile:
    """The worker translator prompt must address the
    'write prior output content to a file' task shape so the LLM
    does not fall back to ``touch``/empty redirects.
    """

    def test_prompt_mentions_heredoc_for_writing_prior_output(self):
        """The prompt must explicitly tell the translator to use a
        heredoc when embedding prior task output content into a new
        file. Without this guidance V4-Flash defaults to creating a
        placeholder file and inferring the user wanted a stub."""
        text = _WORKER_MD.read_text(encoding="utf-8").lower()
        # The rule must mention BOTH "prior" / "previous output" and
        # "heredoc" so the LLM associates the two.
        mentions_prior = (
            "prior task output" in text
            or "preceding task output" in text
            or "previous task output" in text
        )
        assert mentions_prior, (
            "worker.md must reference prior/previous task output for "
            "the file-write contract"
        )
        assert "heredoc" in text, (
            "worker.md must mention heredoc when describing how to "
            "embed prior output into a file write (M1615)"
        )

    def test_prompt_forbids_empty_placeholder_for_content_write(self):
        """The prompt must explicitly forbid the failure mode: when
        the task asks to write CONTENT into a file, ``touch`` or an
        empty redirect is wrong."""
        text = _WORKER_MD.read_text(encoding="utf-8").lower()
        # We pin the FORBIDDEN concept: a content-bearing file-write
        # task must not produce an empty file.
        assert "empty file" in text or "empty placeholder" in text or (
            "must not be empty" in text
        ), (
            "worker.md must explicitly forbid the empty-file failure "
            "mode for content-bearing file writes (M1615)"
        )


# ---------------------------------------------------------------------------
# 3. Pipeline-shape invariant — heredoc command produces non-empty file
# ---------------------------------------------------------------------------


class TestSearchToFilePipelineProducesNonEmptyFile:
    """Given a plan whose translation step yields a heredoc file
    write, the resulting file under workspace/pub/ must be non-empty.
    This is the pipeline-shape invariant the F7 test was meant to
    surface — pinning it at the unit tier guards against regressions
    in the worker scaffolding (workspace pathing, heredoc execution,
    auto-publish copy).
    """

    @pytest.mark.asyncio
    async def test_heredoc_translation_writes_non_empty_file(self, tmp_path):
        """A worker that runs ``cat > pub/file.md << 'EOF' … EOF`` in
        a session workspace produces a file under ``pub/`` that is
        non-empty after the bash subprocess returns."""
        from kiso.worker.utils import _session_workspace, _run_subprocess

        with patch("kiso.worker.utils.KISO_DIR", tmp_path):
            workspace = _session_workspace("sess1")
            (workspace / "pub").mkdir(parents=True, exist_ok=True)

            content = (
                "# Top languages 2025\n\n"
                "| Lang | Use |\n|---|---|\n"
                "| Python | AI/data |\n| JavaScript | web |\n| Go | cloud |\n"
            )
            command = (
                "cat > pub/languages.md << 'EOF'\n"
                + content
                + "EOF"
            )
            stdout, stderr, _truncated, returncode = await _run_subprocess(
                command, env={"PATH": "/usr/bin:/bin"},
                cwd=str(workspace), shell=True,
            )
            assert returncode == 0, (
                f"heredoc command failed: stderr={stderr!r}"
            )

            target = workspace / "pub" / "languages.md"
            assert target.is_file(), "pub/languages.md not created"
            text = target.read_text(encoding="utf-8")
            assert text.strip(), (
                "pub/languages.md is empty — the F7 0-byte regression"
            )
            for token in ("Python", "JavaScript", "Go"):
                assert token in text, (
                    f"file content missing {token!r}; heredoc did not "
                    f"embed the search content"
                )
