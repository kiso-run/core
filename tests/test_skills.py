"""Tests for kiso/skills.py — skill discovery, validation, and execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiso.skills import (
    MAX_ARGS_DEPTH,
    MAX_ARGS_SIZE,
    _check_args_depth,
    _coerce_value,
    _env_var_name,
    _validate_manifest,
    build_planner_skill_list,
    build_skill_env,
    build_skill_input,
    check_deps,
    discover_skills,
    validate_skill_args,
)


# --- Helpers ---

MINIMAL_TOML = """\
[kiso]
type = "skill"
name = "echo"
version = "0.1.0"
description = "Echo skill"

[kiso.skill]
summary = "Echoes input back"

[kiso.skill.args]
text = { type = "string", required = true, description = "text to echo" }
"""

FULL_TOML = """\
[kiso]
type = "skill"
name = "search"
version = "0.2.0"
description = "Web search"

[kiso.skill]
summary = "Web search via API"
session_secrets = ["api_token"]

[kiso.skill.args]
query = { type = "string", required = true, description = "search query" }
max_results = { type = "int", required = false, default = 5, description = "max results" }

[kiso.skill.env]
api_key = { required = true }

[kiso.deps]
python = ">=3.11"
bin = ["curl"]
"""


def _create_skill(tmp_path: Path, name: str, toml_content: str) -> Path:
    """Create a skill directory with kiso.toml, run.py, pyproject.toml."""
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "kiso.toml").write_text(toml_content)
    (skill_dir / "run.py").write_text("import json, sys\ndata = json.load(sys.stdin)\nprint(data['args'].get('text', 'ok'))")
    (skill_dir / "pyproject.toml").write_text("[project]\nname = \"test\"\nversion = \"0.1.0\"")
    return skill_dir


# --- _validate_manifest ---

class TestValidateManifest:
    def test_valid_minimal(self, tmp_path):
        _create_skill(tmp_path, "echo", MINIMAL_TOML)
        import tomllib
        with open(tmp_path / "echo" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "echo")
        assert errors == []

    def test_missing_kiso_section(self, tmp_path):
        _create_skill(tmp_path, "bad", "[other]\nfoo = 1\n")
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert "missing [kiso] section" in errors

    def test_wrong_type(self, tmp_path):
        toml = MINIMAL_TOML.replace('type = "skill"', 'type = "connector"')
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("kiso.type must be 'skill'" in e for e in errors)

    def test_missing_name(self, tmp_path):
        toml = MINIMAL_TOML.replace('name = "echo"', '')
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("kiso.name is required" in e for e in errors)

    def test_missing_skill_section(self, tmp_path):
        toml = "[kiso]\ntype = \"skill\"\nname = \"x\"\n"
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert "missing [kiso.skill] section" in errors

    def test_missing_summary(self, tmp_path):
        toml = MINIMAL_TOML.replace('summary = "Echoes input back"', '')
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("kiso.skill.summary is required" in e for e in errors)

    def test_invalid_arg_type(self, tmp_path):
        toml = MINIMAL_TOML.replace('type = "string"', 'type = "date"')
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("'date'" in e for e in errors)

    def test_missing_run_py(self, tmp_path):
        skill_dir = tmp_path / "bad"
        skill_dir.mkdir()
        (skill_dir / "kiso.toml").write_text(MINIMAL_TOML)
        (skill_dir / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0.1.0\"")
        import tomllib
        with open(skill_dir / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, skill_dir)
        assert "run.py is missing" in errors

    def test_missing_pyproject(self, tmp_path):
        skill_dir = tmp_path / "bad"
        skill_dir.mkdir()
        (skill_dir / "kiso.toml").write_text(MINIMAL_TOML)
        (skill_dir / "run.py").write_text("pass")
        import tomllib
        with open(skill_dir / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, skill_dir)
        assert "pyproject.toml is missing" in errors

    def test_args_not_a_table(self, tmp_path):
        toml = (
            '[kiso]\ntype = "skill"\nname = "x"\n'
            '[kiso.skill]\nsummary = "X"\nargs = "not_a_table"\n'
        )
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("[kiso.skill.args] must be a table" in e for e in errors)

    def test_arg_not_a_table(self, tmp_path):
        toml = MINIMAL_TOML.replace(
            'text = { type = "string", required = true, description = "text to echo" }',
            'text = "not_a_table"',
        )
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("arg 'text' must be a table" in e for e in errors)

    def test_env_not_a_table(self, tmp_path):
        toml = (
            '[kiso]\ntype = "skill"\nname = "x"\n'
            '[kiso.skill]\nsummary = "X"\nenv = "not_a_table"\n'
        )
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("[kiso.skill.env] must be a table" in e for e in errors)

    def test_invalid_session_secrets_type(self, tmp_path):
        toml = MINIMAL_TOML + '\nsession_secrets = "not_a_list"\n'
        # Need to put it under [kiso.skill]
        toml = MINIMAL_TOML.replace(
            "[kiso.skill.args]",
            'session_secrets = "not_a_list"\n\n[kiso.skill.args]',
        )
        _create_skill(tmp_path, "bad", toml)
        import tomllib
        with open(tmp_path / "bad" / "kiso.toml", "rb") as f:
            manifest = tomllib.load(f)
        errors = _validate_manifest(manifest, tmp_path / "bad")
        assert any("session_secrets must be a list" in e for e in errors)


# --- _env_var_name ---

class TestEnvVarName:
    def test_basic(self):
        assert _env_var_name("search", "api_key") == "KISO_SKILL_SEARCH_API_KEY"

    def test_with_dashes(self):
        assert _env_var_name("my-skill", "auth-token") == "KISO_SKILL_MY_SKILL_AUTH_TOKEN"

    def test_uppercase(self):
        assert _env_var_name("Echo", "Key") == "KISO_SKILL_ECHO_KEY"


# --- discover_skills ---

class TestDiscoverSkills:
    def test_empty_dir(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        result = discover_skills(skills_dir)
        assert result == []

    def test_nonexistent_dir(self, tmp_path):
        result = discover_skills(tmp_path / "nonexistent")
        assert result == []

    def test_discovers_valid_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "echo", MINIMAL_TOML)
        result = discover_skills(skills_dir)
        assert len(result) == 1
        assert result[0]["name"] == "echo"
        assert result[0]["summary"] == "Echoes input back"
        assert "text" in result[0]["args_schema"]

    def test_skips_installing(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_dir = _create_skill(skills_dir, "echo", MINIMAL_TOML)
        (skill_dir / ".installing").touch()
        result = discover_skills(skills_dir)
        assert result == []

    def test_skips_no_kiso_toml(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "empty").mkdir()
        result = discover_skills(skills_dir)
        assert result == []

    def test_skips_invalid_manifest(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        bad_dir = skills_dir / "bad"
        bad_dir.mkdir()
        (bad_dir / "kiso.toml").write_text("[kiso]\ntype = \"connector\"\n")
        (bad_dir / "run.py").write_text("pass")
        (bad_dir / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0.1.0\"")
        result = discover_skills(skills_dir)
        assert result == []

    def test_skips_corrupt_toml(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        bad_dir = skills_dir / "corrupt"
        bad_dir.mkdir()
        (bad_dir / "kiso.toml").write_text("this is not valid toml {{{{")
        (bad_dir / "run.py").write_text("pass")
        (bad_dir / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0.1.0\"")
        result = discover_skills(skills_dir)
        assert result == []

    def test_multiple_skills_sorted(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "beta", MINIMAL_TOML.replace('name = "echo"', 'name = "beta"'))
        _create_skill(skills_dir, "alpha", MINIMAL_TOML.replace('name = "echo"', 'name = "alpha"'))
        result = discover_skills(skills_dir)
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "beta"

    def test_skips_files_not_dirs(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "random_file.txt").touch()
        _create_skill(skills_dir, "echo", MINIMAL_TOML)
        result = discover_skills(skills_dir)
        assert len(result) == 1

    def test_full_skill_fields(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "search", FULL_TOML)
        result = discover_skills(skills_dir)
        assert len(result) == 1
        s = result[0]
        assert s["name"] == "search"
        assert s["version"] == "0.2.0"
        assert s["description"] == "Web search"
        assert s["session_secrets"] == ["api_token"]
        assert "api_key" in s["env"]
        assert "query" in s["args_schema"]
        assert "max_results" in s["args_schema"]

    def test_duplicate_skill_name_skipped(self, tmp_path):
        """Two dirs with same kiso.name → only first returned."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "alpha-echo", MINIMAL_TOML)
        _create_skill(skills_dir, "beta-echo", MINIMAL_TOML)
        result = discover_skills(skills_dir)
        assert len(result) == 1
        assert result[0]["name"] == "echo"
        # Should come from alpha-echo (sorted first)
        assert "alpha-echo" in result[0]["path"]


# --- check_deps ---

class TestCheckDeps:
    def test_no_deps(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "echo", MINIMAL_TOML)
        skill = discover_skills(skills_dir)[0]
        result = check_deps(skill)
        assert result == []

    def test_existing_bin(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        _create_skill(skills_dir, "search", FULL_TOML)
        skill = discover_skills(skills_dir)[0]
        # curl should be available in most test environments
        result = check_deps(skill)
        # Don't assert empty — curl might not be installed, just ensure it returns a list
        assert isinstance(result, list)

    def test_missing_bin(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = ["nonexistent_binary_xyz"]')
        _create_skill(skills_dir, "search", toml)
        skill = discover_skills(skills_dir)[0]
        result = check_deps(skill)
        assert "nonexistent_binary_xyz" in result

    def test_bin_not_a_list(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        toml = FULL_TOML.replace('bin = ["curl"]', 'bin = "curl"')
        _create_skill(skills_dir, "search", toml)
        skill = discover_skills(skills_dir)[0]
        result = check_deps(skill)
        assert result == []


# --- build_planner_skill_list ---

class TestBuildPlannerSkillList:
    def _make_skill(self, name="echo", summary="Echo skill", args_schema=None):
        return {
            "name": name,
            "summary": summary,
            "args_schema": args_schema or {"text": {"type": "string", "required": True, "description": "input text"}},
            "env": {},
            "session_secrets": [],
            "path": "/fake",
            "version": "0.1.0",
            "description": "",
        }

    def test_empty_skills(self):
        assert build_planner_skill_list([]) == ""

    def test_admin_sees_all(self):
        skills = [self._make_skill("a", "Skill A"), self._make_skill("b", "Skill B")]
        result = build_planner_skill_list(skills, "admin")
        assert "- a — Skill A" in result
        assert "- b — Skill B" in result

    def test_user_star_sees_all(self):
        skills = [self._make_skill("a", "Skill A")]
        result = build_planner_skill_list(skills, "user", "*")
        assert "- a — Skill A" in result

    def test_user_list_filters(self):
        skills = [self._make_skill("a", "Skill A"), self._make_skill("b", "Skill B")]
        result = build_planner_skill_list(skills, "user", ["a"])
        assert "- a — Skill A" in result
        assert "- b — Skill B" not in result

    def test_user_empty_list(self):
        skills = [self._make_skill("a", "Skill A")]
        result = build_planner_skill_list(skills, "user", [])
        assert result == ""

    def test_user_none_skills(self):
        skills = [self._make_skill("a", "Skill A")]
        result = build_planner_skill_list(skills, "user", None)
        assert result == ""

    def test_includes_args_schema(self):
        schema = {
            "query": {"type": "string", "required": True, "description": "search query"},
            "limit": {"type": "int", "required": False, "default": 5, "description": "max results"},
        }
        skills = [self._make_skill("search", "Search", schema)]
        result = build_planner_skill_list(skills, "admin")
        assert "query (string, required): search query" in result
        assert "limit (int, optional, default=5): max results" in result

    def test_header_present(self):
        skills = [self._make_skill()]
        result = build_planner_skill_list(skills, "admin")
        assert result.startswith("Available skills:")


# --- validate_skill_args ---

class TestValidateSkillArgs:
    SCHEMA = {
        "query": {"type": "string", "required": True},
        "limit": {"type": "int", "required": False},
        "ratio": {"type": "float", "required": False},
        "verbose": {"type": "bool", "required": False},
    }

    def test_valid_required_only(self):
        errors = validate_skill_args({"query": "test"}, self.SCHEMA)
        assert errors == []

    def test_valid_all_args(self):
        errors = validate_skill_args(
            {"query": "test", "limit": 10, "ratio": 0.5, "verbose": True},
            self.SCHEMA,
        )
        assert errors == []

    def test_missing_required(self):
        errors = validate_skill_args({}, self.SCHEMA)
        assert any("missing required arg: query" in e for e in errors)

    def test_wrong_type_string(self):
        errors = validate_skill_args({"query": 123}, self.SCHEMA)
        assert any("expected string" in e for e in errors)

    def test_wrong_type_int(self):
        errors = validate_skill_args({"query": "ok", "limit": "ten"}, self.SCHEMA)
        assert any("expected int" in e for e in errors)

    def test_bool_not_int(self):
        errors = validate_skill_args({"query": "ok", "limit": True}, self.SCHEMA)
        assert any("expected int, got bool" in e for e in errors)

    def test_bool_not_float(self):
        errors = validate_skill_args({"query": "ok", "ratio": True}, self.SCHEMA)
        assert any("expected float, got bool" in e for e in errors)

    def test_int_as_float(self):
        errors = validate_skill_args({"query": "ok", "ratio": 5}, self.SCHEMA)
        assert errors == []  # int is valid as float

    def test_wrong_type_bool(self):
        errors = validate_skill_args({"query": "ok", "verbose": "yes"}, self.SCHEMA)
        assert any("expected bool" in e for e in errors)

    def test_unknown_args_allowed(self):
        errors = validate_skill_args({"query": "ok", "extra": "fine"}, self.SCHEMA)
        assert errors == []

    def test_max_size_exceeded(self):
        big_value = "x" * (MAX_ARGS_SIZE + 1)
        errors = validate_skill_args({"query": big_value}, self.SCHEMA)
        assert any("exceeds" in e for e in errors)

    def test_max_depth_exceeded(self):
        # Build nested dict exceeding MAX_ARGS_DEPTH
        nested: dict = {"query": "ok"}
        current = nested
        for _ in range(MAX_ARGS_DEPTH + 2):
            current["nested"] = {}
            current = current["nested"]
        errors = validate_skill_args(nested, self.SCHEMA)
        assert any("depth" in e for e in errors)

    def test_empty_args_empty_schema(self):
        errors = validate_skill_args({}, {})
        assert errors == []


# --- _check_args_depth ---

class TestCheckArgsDepth:
    def test_flat(self):
        assert _check_args_depth({"a": 1, "b": "two"}) is True

    def test_within_limit(self):
        obj = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        assert _check_args_depth(obj) is True

    def test_exceeds_limit(self):
        obj: dict = {}
        current = obj
        for _ in range(MAX_ARGS_DEPTH + 2):
            current["x"] = {}
            current = current["x"]
        assert _check_args_depth(obj) is False

    def test_list_depth(self):
        obj = {"a": [[[[[["deep"]]]]]]}
        assert _check_args_depth(obj) is False

    def test_scalar(self):
        assert _check_args_depth("hello") is True
        assert _check_args_depth(42) is True


# --- _coerce_value ---

class TestCoerceValue:
    def test_string_ok(self):
        assert _coerce_value("hello", "string") == "hello"

    def test_string_wrong(self):
        with pytest.raises(ValueError, match="expected string"):
            _coerce_value(123, "string")

    def test_int_ok(self):
        assert _coerce_value(42, "int") == 42

    def test_int_bool_rejected(self):
        with pytest.raises(ValueError, match="expected int, got bool"):
            _coerce_value(True, "int")

    def test_int_wrong(self):
        with pytest.raises(ValueError, match="expected int"):
            _coerce_value("42", "int")

    def test_float_ok(self):
        assert _coerce_value(3.14, "float") == 3.14

    def test_float_from_int(self):
        assert _coerce_value(5, "float") == 5.0

    def test_float_bool_rejected(self):
        with pytest.raises(ValueError, match="expected float, got bool"):
            _coerce_value(False, "float")

    def test_float_string_rejected(self):
        with pytest.raises(ValueError, match="expected float"):
            _coerce_value("3.14", "float")

    def test_bool_ok(self):
        assert _coerce_value(True, "bool") is True

    def test_bool_wrong(self):
        with pytest.raises(ValueError, match="expected bool"):
            _coerce_value(1, "bool")

    def test_unknown_type(self):
        with pytest.raises(ValueError, match="unknown type"):
            _coerce_value("x", "date")


# --- build_skill_input ---

class TestBuildSkillInput:
    def _make_skill(self, session_secrets=None):
        return {
            "name": "echo",
            "summary": "Echo",
            "args_schema": {},
            "env": {},
            "session_secrets": session_secrets or [],
            "path": "/fake",
            "version": "0.1.0",
            "description": "",
        }

    def test_basic_input(self):
        skill = self._make_skill()
        result = build_skill_input(skill, {"text": "hi"}, "sess1", "/workspace")
        assert result["args"] == {"text": "hi"}
        assert result["session"] == "sess1"
        assert result["workspace"] == "/workspace"
        assert result["session_secrets"] == {}
        assert result["plan_outputs"] == []

    def test_with_plan_outputs(self):
        skill = self._make_skill()
        outputs = [{"index": 1, "type": "exec", "detail": "ls", "output": "a\nb", "status": "done"}]
        result = build_skill_input(skill, {}, "sess1", "/ws", plan_outputs=outputs)
        assert result["plan_outputs"] == outputs

    def test_scoped_session_secrets(self):
        skill = self._make_skill(session_secrets=["api_token"])
        secrets = {"api_token": "tok_123", "other_secret": "should_not_appear"}
        result = build_skill_input(skill, {}, "sess1", "/ws", session_secrets=secrets)
        assert result["session_secrets"] == {"api_token": "tok_123"}
        assert "other_secret" not in result["session_secrets"]

    def test_no_declared_secrets_scoped_empty(self):
        skill = self._make_skill(session_secrets=[])
        secrets = {"api_token": "tok_123"}
        result = build_skill_input(skill, {}, "sess1", "/ws", session_secrets=secrets)
        assert result["session_secrets"] == {}

    def test_none_session_secrets(self):
        skill = self._make_skill(session_secrets=["api_token"])
        result = build_skill_input(skill, {}, "sess1", "/ws", session_secrets=None)
        assert result["session_secrets"] == {}


# --- build_skill_env ---

class TestBuildSkillEnv:
    def test_basic_env(self):
        skill = {"name": "echo", "env": {}}
        env = build_skill_env(skill)
        assert "PATH" in env
        assert len(env) == 1

    def test_env_var_present(self):
        skill = {"name": "search", "env": {"api_key": {"required": True}}}
        with patch.dict(os.environ, {"KISO_SKILL_SEARCH_API_KEY": "sk-123"}):
            env = build_skill_env(skill)
        assert env["KISO_SKILL_SEARCH_API_KEY"] == "sk-123"

    def test_env_var_missing_required(self):
        skill = {"name": "search", "env": {"api_key": {"required": True}}}
        # Remove the var if set
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_skill_env(skill)
        # Should not include missing var, just PATH
        assert "KISO_SKILL_SEARCH_API_KEY" not in env

    def test_env_var_missing_optional(self):
        skill = {"name": "search", "env": {"api_key": {"required": False}}}
        with patch.dict(os.environ, {}, clear=True):
            os.environ["PATH"] = "/usr/bin"
            env = build_skill_env(skill)
        assert "KISO_SKILL_SEARCH_API_KEY" not in env

    def test_multiple_env_vars(self):
        skill = {
            "name": "search",
            "env": {
                "api_key": {"required": True},
                "token": {"required": False},
            },
        }
        with patch.dict(os.environ, {
            "KISO_SKILL_SEARCH_API_KEY": "key1",
            "KISO_SKILL_SEARCH_TOKEN": "tok1",
        }):
            env = build_skill_env(skill)
        assert env["KISO_SKILL_SEARCH_API_KEY"] == "key1"
        assert env["KISO_SKILL_SEARCH_TOKEN"] == "tok1"
