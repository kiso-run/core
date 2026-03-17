"""M676: Tests for knowledge markdown import parser."""

from __future__ import annotations

import argparse
from unittest.mock import patch, MagicMock

import pytest

from kiso.knowledge_import import parse_knowledge_markdown, ImportedFact
from tests._cli_test_helpers import mock_cli_config


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
             patch("kiso.config.load_config", return_value=mock_cli_config()), \
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


# --- M676: Nested headings and mixed sections ---


class TestNestedHeadingsAndMixedSections:
    def test_sub_heading_under_entity_keeps_entity_context(self):
        """### Sub-heading under ## Entity: preserves entity context for facts.
        Since _HEADING_RE only matches `## ` (not `### `), the sub-heading line
        is treated as plain content and becomes a fact itself."""
        md = """\
## Entity: my-app (project)

- Top-level fact about the app

### Architecture

- Uses microservices with message queues
- Deploys on Kubernetes cluster setup
"""
        facts = parse_knowledge_markdown(md)
        # 4 facts: bullet, "### Architecture" as plain text, 2 more bullets
        assert len(facts) == 4
        assert all(f.entity_name == "my-app" for f in facts)
        assert all(f.entity_kind == "project" for f in facts)

    def test_behaviors_after_entity_resets_entity(self):
        """## Behaviors after ## Entity: clears entity context."""
        md = """\
## Entity: backend (service)

- Runs on port 8080 with FastAPI

## Behaviors

- Always validate input parameters
- Never return raw stack traces
"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 3
        # First fact has entity
        assert facts[0].entity_name == "backend"
        assert facts[0].entity_kind == "service"
        assert facts[0].category == "general"
        # Behavior facts have no entity
        assert facts[1].category == "behavior"
        assert facts[1].entity_name is None
        assert facts[2].category == "behavior"
        assert facts[2].entity_name is None

    def test_entity_after_behaviors_resets_category(self):
        """## Entity: after ## Behaviors resets category back to default."""
        md = """\
## Behaviors

- Always be concise in responses

## Entity: frontend (project)

- Uses React 18 with TypeScript config
"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 2
        assert facts[0].category == "behavior"
        assert facts[0].entity_name is None
        assert facts[1].category == "general"
        assert facts[1].entity_name == "frontend"

    def test_generic_heading_resets_entity_context(self):
        """A generic ## heading (not Entity/Behaviors) resets entity context."""
        md = """\
## Entity: my-db (database)

- PostgreSQL 15 with pgvector extension

## General Notes

- This project was started in 2024
"""
        facts = parse_knowledge_markdown(md)
        assert len(facts) == 2
        assert facts[0].entity_name == "my-db"
        assert facts[1].entity_name is None
        assert facts[1].category == "general"


# --- M677: Round-trip export → import ---


class TestRoundTripExportImport:
    def _format_facts_as_markdown(self, facts_dicts: list[dict]) -> str:
        """Reproduce the markdown export logic from cli/knowledge.py."""
        lines: list[str] = []
        by_entity: dict[str, list[dict]] = {}
        no_entity: list[dict] = []
        for f in facts_dicts:
            ename = f.get("entity_name")
            if ename:
                by_entity.setdefault(ename, []).append(f)
            else:
                no_entity.append(f)

        for ename, efacts in sorted(by_entity.items()):
            kind = efacts[0].get("entity_kind") or "concept"
            lines.append(f"## Entity: {ename} ({kind})\n")
            for f in efacts:
                tags = " ".join(f"#{t}" for t in f.get("tags", []))
                cat_note = f" [{f['category']}]" if f.get("category") not in ("general", None) else ""
                lines.append(f"- {f['content']}{cat_note} {tags}".rstrip())
            lines.append("")

        if no_entity:
            behavior_facts = [f for f in no_entity if f.get("category") == "behavior"]
            other_facts = [f for f in no_entity if f.get("category") != "behavior"]
            if behavior_facts:
                lines.append("## Behaviors\n")
                for f in behavior_facts:
                    tags = " ".join(f"#{t}" for t in f.get("tags", []))
                    lines.append(f"- {f['content']} {tags}".rstrip())
                lines.append("")
            if other_facts:
                lines.append("## General\n")
                for f in other_facts:
                    tags = " ".join(f"#{t}" for t in f.get("tags", []))
                    cat_note = f" [{f['category']}]" if f.get("category") not in ("general", None) else ""
                    lines.append(f"- {f['content']}{cat_note} {tags}".rstrip())
                lines.append("")

        return "\n".join(lines)

    def test_roundtrip_entity_facts(self):
        """Export entity-grouped facts as markdown, re-import, verify match."""
        original_facts = [
            {
                "content": "Uses PostgreSQL 15 with pgvector",
                "category": "general",
                "entity_name": "backend-api",
                "entity_kind": "project",
                "tags": ["postgresql", "database"],
            },
            {
                "content": "Deployed on AWS ECS Fargate setup",
                "category": "general",
                "entity_name": "backend-api",
                "entity_kind": "project",
                "tags": ["aws", "deploy"],
            },
        ]
        md = self._format_facts_as_markdown(original_facts)
        imported = parse_knowledge_markdown(md)

        assert len(imported) == 2
        for orig, imp in zip(original_facts, imported):
            assert imp.content == orig["content"]
            assert imp.entity_name == orig["entity_name"]
            assert imp.entity_kind == orig["entity_kind"]
            assert set(imp.tags) == set(orig["tags"])

    def test_roundtrip_behavior_facts(self):
        """Export behavior facts, re-import, verify category preserved."""
        original_facts = [
            {
                "content": "Always use concrete metrics in responses",
                "category": "behavior",
                "entity_name": None,
                "tags": [],
            },
            {
                "content": "Format tabular data as markdown tables",
                "category": "behavior",
                "entity_name": None,
                "tags": ["formatting"],
            },
        ]
        md = self._format_facts_as_markdown(original_facts)
        imported = parse_knowledge_markdown(md)

        assert len(imported) == 2
        for imp in imported:
            assert imp.category == "behavior"
            assert imp.entity_name is None

        assert imported[0].content == "Always use concrete metrics in responses"
        assert imported[1].content == "Format tabular data as markdown tables"
        assert "formatting" in imported[1].tags

    def test_roundtrip_mixed_entities_and_behaviors(self):
        """Full round-trip with entities + behaviors + general facts."""
        original_facts = [
            {
                "content": "Backend runs on port 8000 with FastAPI",
                "category": "general",
                "entity_name": "backend",
                "entity_kind": "service",
                "tags": ["config"],
            },
            {
                "content": "Always validate user input parameters",
                "category": "behavior",
                "entity_name": None,
                "tags": [],
            },
            {
                "content": "Project started in January 2024",
                "category": "general",
                "entity_name": None,
                "tags": ["history"],
            },
        ]
        md = self._format_facts_as_markdown(original_facts)
        imported = parse_knowledge_markdown(md)

        assert len(imported) == 3
        # Entity fact
        entity_facts = [f for f in imported if f.entity_name == "backend"]
        assert len(entity_facts) == 1
        assert entity_facts[0].content == "Backend runs on port 8000 with FastAPI"
        assert "config" in entity_facts[0].tags
        # Behavior fact
        beh_facts = [f for f in imported if f.category == "behavior"]
        assert len(beh_facts) == 1
        assert "validate" in beh_facts[0].content
        # General fact (no entity)
        gen_facts = [f for f in imported if f.category == "general" and f.entity_name is None]
        assert len(gen_facts) == 1
        assert "2024" in gen_facts[0].content
