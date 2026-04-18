"""Tests for kiso-migrate-recipes-to-skills (M1541)."""
from __future__ import annotations

from pathlib import Path

import pytest

from kiso.migrate_recipes import migrate_recipes


def _write_recipe(
    dir_: Path, name: str, *, summary: str, body: str,
    applies_to: str = "", excludes: str = "",
) -> Path:
    lines = ["---", f"name: {name}", f"summary: {summary}"]
    if applies_to:
        lines.append(f"applies_to: {applies_to}")
    if excludes:
        lines.append(f"excludes: {excludes}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestMigrateRecipes:
    def test_basic_recipe_becomes_skill(self, tmp_path):
        recipes = tmp_path / "recipes"
        skills = tmp_path / "skills"
        _write_recipe(recipes, "debug-py", summary="Python debugging", body="Use pdb.")
        summary = migrate_recipes(recipes_dir=recipes, skills_dir=skills)
        assert summary["migrated"] == ["debug-py"]
        assert summary["skipped_existing"] == []
        out = (skills / "debug-py" / "SKILL.md").read_text()
        assert "name: debug-py" in out
        assert "description: Python debugging" in out
        assert "## Planner" in out
        assert "Use pdb." in out

    def test_applies_to_excludes_mapped_to_activation_hints(self, tmp_path):
        recipes = tmp_path / "recipes"
        skills = tmp_path / "skills"
        _write_recipe(
            recipes, "only-py", summary="s", body="b",
            applies_to="python, fastapi", excludes="javascript",
        )
        migrate_recipes(recipes_dir=recipes, skills_dir=skills)
        out = (skills / "only-py" / "SKILL.md").read_text()
        assert "activation_hints:" in out
        assert "  applies_to:" in out
        assert "    - python" in out
        assert "    - fastapi" in out
        assert "  excludes:" in out
        assert "    - javascript" in out

    def test_idempotent_skips_existing(self, tmp_path):
        recipes = tmp_path / "recipes"
        skills = tmp_path / "skills"
        _write_recipe(recipes, "x", summary="s", body="first")
        migrate_recipes(recipes_dir=recipes, skills_dir=skills)

        # Hand-edit the produced skill and rerun — the edit must survive.
        target = skills / "x" / "SKILL.md"
        target.write_text(target.read_text() + "\n# user edit\n")
        summary = migrate_recipes(recipes_dir=recipes, skills_dir=skills)
        assert summary["migrated"] == []
        assert summary["skipped_existing"] == ["x"]
        assert "# user edit" in target.read_text()

    def test_overwrite_replaces_existing(self, tmp_path):
        recipes = tmp_path / "recipes"
        skills = tmp_path / "skills"
        _write_recipe(recipes, "x", summary="s", body="first")
        migrate_recipes(recipes_dir=recipes, skills_dir=skills)
        target = skills / "x" / "SKILL.md"
        target.write_text("# hand written\n")
        summary = migrate_recipes(
            recipes_dir=recipes, skills_dir=skills, overwrite=True,
        )
        assert summary["migrated"] == ["x"]
        assert "hand written" not in target.read_text()

    def test_missing_recipes_dir_returns_cleanly(self, tmp_path):
        skills = tmp_path / "skills"
        summary = migrate_recipes(
            recipes_dir=tmp_path / "does-not-exist",
            skills_dir=skills,
        )
        assert summary["source_missing"] is True
        assert summary["migrated"] == []

    def test_migrated_skill_is_discoverable_by_loader(self, tmp_path):
        """The generated SKILL.md must parse cleanly via the M1500 loader."""
        from kiso.skill_loader import discover_skills

        recipes = tmp_path / "recipes"
        skills = tmp_path / "skills"
        _write_recipe(
            recipes, "hello",
            summary="Say hello",
            body="When the user greets you, respond in kind.",
            applies_to="greeting, hello",
        )
        migrate_recipes(recipes_dir=recipes, skills_dir=skills)

        loaded = discover_skills(skills_dir=skills)
        names = [s.name for s in loaded]
        assert "hello" in names
        hello = next(s for s in loaded if s.name == "hello")
        assert hello.description == "Say hello"
        assert hello.activation_hints == {
            "applies_to": ["greeting", "hello"],
            "excludes": [],
        }
        # The recipe body is preserved under the `## Planner` role section.
        assert "planner" in hello.role_sections
        assert "respond in kind" in hello.role_sections["planner"]


class TestMigrateRecipesCLI:
    def test_help_runs(self):
        from kiso.migrate_recipes import main
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_cli_end_to_end(self, tmp_path, capsys):
        from kiso.migrate_recipes import main

        recipes = tmp_path / "recipes"
        skills = tmp_path / "skills"
        _write_recipe(recipes, "r1", summary="x", body="y")
        rc = main([
            "--recipes-dir", str(recipes),
            "--skills-dir", str(skills),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Migrated 1 recipe(s)" in out
        assert "+ r1" in out
