"""Retirement invariants for the ``search`` task type.

Once part 2a of the wrapper/search retirement lands, these tests pin
the absence of the ``search`` surface and should stay green forever.
"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module / file surface
# ---------------------------------------------------------------------------


class TestSearchSurfaceGone:

    def test_worker_search_module_deleted(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("kiso.worker.search")

    def test_searcher_role_prompt_deleted(self):
        import kiso
        kiso_root = Path(kiso.__file__).parent
        assert not (kiso_root / "roles" / "searcher.md").exists()

    def test_brain_does_not_export_searcher(self):
        import kiso.brain as brain
        assert not hasattr(brain, "SearcherError")
        assert not hasattr(brain, "run_searcher")

    def test_brain_common_does_not_define_task_type_search(self):
        import kiso.brain.common as common
        assert not hasattr(common, "TASK_TYPE_SEARCH")
        assert "search" not in common.TASK_TYPES


# ---------------------------------------------------------------------------
# Planner / validator
# ---------------------------------------------------------------------------


class TestValidatePlanRejectsSearch:

    def _minimal_plan(self, task_type: str) -> dict:
        return {
            "goal": "x",
            "tasks": [
                {
                    "type": task_type,
                    "detail": "do thing",
                    "wrapper": None,
                    "args": None,
                    "expect": "done",
                }
            ],
        }

    def test_validate_plan_rejects_type_search(self):
        from kiso.brain.planner import validate_plan
        errors = validate_plan(self._minimal_plan("search"))
        assert errors, "expected validate_plan to reject type=search"
        assert any("search" in e.lower() or "type" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Config: searcher model removed
# ---------------------------------------------------------------------------


class TestSearcherModelRemoved:

    def test_model_defaults_does_not_contain_searcher(self):
        from kiso.config import MODEL_DEFAULTS
        assert "searcher" not in MODEL_DEFAULTS

    def test_config_template_does_not_reference_searcher(self):
        from kiso.config import CONFIG_TEMPLATE
        assert "searcher" not in CONFIG_TEMPLATE.lower()

    def test_config_parser_rejects_legacy_searcher_line(self, tmp_path):
        from kiso.config import ConfigError, reload_config

        # Minimal valid config that still includes a legacy searcher entry
        # under [models]. The v0.10 config validator must reject it with a
        # clear error — silent-ignore would be a footgun.
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[tokens]\n'
            'dev = "test"\n'
            '[providers.openrouter]\n'
            'base_url = "https://openrouter.ai/api/v1"\n'
            '[users.dev]\n'
            'role = "admin"\n'
            '[models]\n'
            'searcher = "perplexity/sonar"\n',
            encoding="utf-8",
        )

        with pytest.raises(ConfigError) as exc_info:
            reload_config(config_path)
        msg = str(exc_info.value).lower()
        assert "searcher" in msg


# ---------------------------------------------------------------------------
# Roles registry: searcher entry gone
# ---------------------------------------------------------------------------


class TestRolesRegistrySearcherGone:

    def test_searcher_role_removed_from_registry(self):
        from kiso.brain.roles_registry import ROLES
        assert "searcher" not in ROLES
