"""M676: Tests for knowledge markdown import parser."""

from __future__ import annotations

import argparse
from unittest.mock import patch, MagicMock

import pytest

from kiso.knowledge_import import parse_knowledge_markdown, ImportedFact


class TestParseKnowledgeMarkdown:
    def test_entity_heading_with_bullets(self):
        md = """\
## Entity: my-app (project)

- Backend uses FastAPI with PostgreSQL #python #backend
- Deploy on AWS ECS with Fargate #aws #deploy
"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 2
        assert facts[0].entity_name == "my-app"
        assert facts[0].entity_kind == "project"
        assert facts[0].content == "Backend uses FastAPI with PostgreSQL"
        assert "python" in facts[0].tags
        assert "backend" in facts[0].tags
        assert facts[1].content == "Deploy on AWS ECS with Fargate"
        assert "aws" in facts[1].tags

    def test_behaviors_section(self):
        md = """\
## Behaviors

- Always use concrete metrics and data
- Format results as markdown tables
"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 2
        assert all(f.category == "behavior" for f in facts)
        assert facts[0].entity_name is None

    def test_mixed_sections(self):
        md = """\
## Entity: backend-api (project)

- Runs on port 8000 #config

## Behaviors

- Always respond formally

## Entity: frontend (project)

- Uses React with TypeScript #react #typescript
"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 3
        assert facts[0].entity_name == "backend-api"
        assert facts[0].category == "general"
        assert facts[1].category == "behavior"
        assert facts[1].entity_name is None
        assert facts[2].entity_name == "frontend"
        assert facts[2].category == "general"

    def test_default_category_override(self):
        md = "- Some project fact about architecture"
        facts = parse_knowledge_markdown(md, default_category="project")
        assert len(facts) == 1
        assert facts[0].category == "project"

    def test_empty_lines_skipped(self):
        md = """\

- Fact one

- Fact two

"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 2

    def test_short_content_skipped(self):
        md = "- Hi\n- This is a real fact about the system"
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 1
        assert "real fact" in facts[0].content

    def test_no_facts_returns_empty(self):
        md = "## Some heading\n\n## Another heading"
        facts = parse_knowledge_markdown(md)
        assert facts == []

    def test_plain_paragraph_becomes_fact(self):
        md = "The project uses a microservices architecture with 5 services."
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 1
        assert "microservices" in facts[0].content

    def test_tags_extracted_and_removed_from_content(self):
        md = "- Uses Redis for caching #redis #cache #performance"
        facts = parse_knowledge_markdown(md)
        assert facts[0].content == "Uses Redis for caching"
        assert set(facts[0].tags) == {"redis", "cache", "performance"}

    def test_entity_heading_case_insensitive(self):
        md = "## entity: My-App (Project)\n\n- Some fact about the app"
        facts = parse_knowledge_markdown(md)
        assert facts[0].entity_name == "My-App"
        assert facts[0].entity_kind == "Project"

    def test_asterisk_bullets(self):
        md = "* Fact with asterisk bullet point"
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 1
        assert "asterisk" in facts[0].content


class TestKnowledgeImportCLI:
    def _mock_config(self):
        cfg = MagicMock()
        cfg.tokens = {"cli": "tok-abc"}
        return cfg

    def test_dry_run(self, capsys, tmp_path):
        from cli.knowledge import knowledge_import
        md_file = tmp_path / "context.md"
        md_file.write_text("## Entity: app (project)\n\n- Uses Flask #python\n")
        args = argparse.Namespace(
            api="http://localhost:8333", file=str(md_file),
            category=None, dry_run=True,
        )
        with patch("cli.plugin_ops.require_admin"):
            knowledge_import(args)
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "Uses Flask" in out
        assert "#python" in out

    def test_import_calls_api(self, tmp_path):
        from cli.knowledge import knowledge_import
        md_file = tmp_path / "context.md"
        md_file.write_text("- Fact A about the system\n- Fact B about the system\n")
        args = argparse.Namespace(
            api="http://localhost:8333", file=str(md_file),
            category=None, dry_run=False,
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 1, "content": "x", "category": "general"}
        mock_resp.raise_for_status = MagicMock()
        with patch("cli.plugin_ops.require_admin"), \
             patch("kiso.config.load_config", return_value=self._mock_config()), \
             patch("httpx.request", return_value=mock_resp) as mock_req:
            knowledge_import(args)
        # Two facts → two POST calls
        assert mock_req.call_count == 2

    def test_file_not_found(self, capsys):
        from cli.knowledge import knowledge_import
        args = argparse.Namespace(
            api="http://localhost:8333", file="/nonexistent/file.md",
            category=None, dry_run=False,
        )
        with patch("cli.plugin_ops.require_admin"), \
             pytest.raises(SystemExit):
            knowledge_import(args)
        assert "not found" in capsys.readouterr().err
