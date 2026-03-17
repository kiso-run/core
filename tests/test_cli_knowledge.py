"""M673: CLI tests for kiso knowledge commands."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from tests._cli_test_helpers import mock_cli_config, make_cli_args, mock_http_response


class TestKnowledgeList:
    def test_list_empty(self, capsys):
        from cli.knowledge import knowledge_list
        args = make_cli_args()
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": []}):
            knowledge_list(args)
        assert "No knowledge facts found" in capsys.readouterr().out

    def test_list_with_facts(self, capsys):
        from cli.knowledge import knowledge_list
        args = make_cli_args()
        facts = [
            {"id": 1, "content": "Uses Flask", "category": "project",
             "entity_name": "my-app", "tags": ["python"]},
            {"id": 2, "content": "Always be concise", "category": "behavior",
             "entity_name": None, "tags": []},
        ]
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": facts}):
            knowledge_list(args)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "Uses Flask" in out
        assert "[my-app]" in out

    def test_list_with_category_filter(self):
        from cli.knowledge import knowledge_list
        args = make_cli_args(category="behavior", entity=None, tag=None, limit=50)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": []}) as mock_req:
            knowledge_list(args)
        call_kwargs = mock_req.return_value  # MagicMock
        # Verify request was made (at least called once)
        assert mock_req.called


class TestKnowledgeAdd:
    def test_add_basic(self, capsys):
        from cli.knowledge import knowledge_add
        args = make_cli_args(content="Project uses microservices",
                          category="general", entity=None, entity_kind=None, tags=None)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"id": 42, "content": "Project uses microservices", "category": "general"}):
            knowledge_add(args)
        out = capsys.readouterr().out
        assert "id=42" in out

    def test_add_empty_content_rejected(self, capsys):
        from cli.knowledge import knowledge_add
        args = make_cli_args(content="  ", category="general",
                          entity=None, entity_kind=None, tags=None)
        with patch("cli.plugin_ops.require_admin"), \
             pytest.raises(SystemExit):
            knowledge_add(args)
        assert "empty" in capsys.readouterr().err


class TestKnowledgeSearch:
    def test_search_no_results(self, capsys):
        from cli.knowledge import knowledge_search
        args = make_cli_args(query="nonexistent topic")
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": []}):
            knowledge_search(args)
        assert "No results" in capsys.readouterr().out

    def test_search_with_results(self, capsys):
        from cli.knowledge import knowledge_search
        args = make_cli_args(query="Flask")
        facts = [{"id": 1, "content": "Uses Flask framework", "category": "tool",
                  "entity_name": "flask", "tags": []}]
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": facts}):
            knowledge_search(args)
        out = capsys.readouterr().out
        assert "Flask" in out


class TestKnowledgeExport:
    def test_export_json(self, capsys):
        from cli.knowledge import knowledge_export
        args = make_cli_args(category=None, entity=None, format="json", output=None)
        facts = [
            {"id": 1, "content": "Uses Flask", "category": "project",
             "entity_name": "app", "entity_kind": "project", "tags": ["python"]},
        ]
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": facts}):
            knowledge_export(args)
        import json
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert len(parsed) == 1
        assert parsed[0]["content"] == "Uses Flask"

    def test_export_markdown(self, capsys):
        from cli.knowledge import knowledge_export
        args = make_cli_args(category=None, entity=None, format="md", output=None)
        facts = [
            {"id": 1, "content": "Uses Flask", "category": "project",
             "entity_name": "app", "entity_kind": "project", "tags": ["python"]},
            {"id": 2, "content": "Always be formal", "category": "behavior",
             "entity_name": None, "tags": []},
        ]
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": facts}):
            knowledge_export(args)
        out = capsys.readouterr().out
        assert "## Entity: app (project)" in out
        assert "Uses Flask" in out
        assert "## Behaviors" in out
        assert "Always be formal" in out

    def test_export_to_file(self, tmp_path):
        from cli.knowledge import knowledge_export
        outfile = tmp_path / "export.json"
        args = make_cli_args(category=None, entity=None, format="json", output=str(outfile))
        facts = [{"id": 1, "content": "Test fact", "category": "general",
                  "entity_name": None, "tags": []}]
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             mock_http_response({"facts": facts}):
            knowledge_export(args)
        assert outfile.exists()
        import json
        data = json.loads(outfile.read_text())
        assert len(data) == 1


class TestKnowledgeRemove:
    def test_remove_success(self, capsys):
        from cli.knowledge import knowledge_remove
        args = make_cli_args(fact_id=42)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"deleted": True}):
            knowledge_remove(args)
        assert "42 removed" in capsys.readouterr().out

    def test_remove_failure(self, capsys):
        from cli.knowledge import knowledge_remove
        args = make_cli_args(fact_id=99)
        with patch("kiso.config.load_config", return_value=mock_cli_config()), \
             patch("cli.plugin_ops.require_admin"), \
             mock_http_response({"deleted": False}), \
             pytest.raises(SystemExit):
            knowledge_remove(args)
        assert "could not remove" in capsys.readouterr().err
