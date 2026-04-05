"""Dependency, file-ref, and workspace-file handoff helpers for worker.loop."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from kiso.worker.utils import (
    _PUB_IGNORE_DIRS,
    _build_execution_state,
    _make_file_ref,
    _session_workspace,
)

_FILE_ARG_NAME_HINTS = (
    "path", "file", "image", "document", "audio", "video", "input",
)
_GLOB_CHARS = frozenset("*?[]")
_PY_IMPORT_RE = re.compile(r"\b(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)")


def _workspace_visible_files(session: str) -> list[str]:
    """Return visible workspace file paths relative to the session root."""
    state = _build_execution_state(session)
    files: list[str] = []
    for entry in state.workspace_files:
        workspace_path = entry.get("workspace_path") or entry.get("path")
        if workspace_path:
            files.append(workspace_path)
    return files


def _extract_known_file_refs(plan_outputs: list[dict]) -> list[dict]:
    """Collect canonical file refs carried by prior plan outputs."""
    refs: list[dict] = []
    seen: set[str] = set()
    for entry in plan_outputs:
        for file_ref in entry.get("file_refs", []) or []:
            if not isinstance(file_ref, dict):
                continue
            key = file_ref.get("file_id") or file_ref.get("abs_path") or file_ref.get("path")
            if not key or key in seen:
                continue
            seen.add(key)
            refs.append(file_ref)
    return refs


def _extract_known_artifact_refs(plan_outputs: list[dict]) -> list[dict]:
    """Collect canonical artifact refs carried by prior plan outputs."""
    refs: list[dict] = []
    seen: set[str] = set()
    for entry in plan_outputs:
        for artifact_ref in entry.get("artifact_refs", []) or []:
            if not isinstance(artifact_ref, dict):
                continue
            key = artifact_ref.get("artifact_id")
            if not key or key in seen:
                continue
            seen.add(key)
            refs.append(artifact_ref)
    return refs


def _infer_task_dependencies(task_row: dict, plan_outputs: list[dict]) -> list[dict]:
    """Infer authoritative file/artifact dependencies for the current task."""
    haystack_parts = [
        str(task_row.get("detail") or ""),
        str(task_row.get("expect") or ""),
    ]
    args_dict = task_row.get("args_dict")
    if isinstance(args_dict, dict):
        haystack_parts.extend(str(v) for v in args_dict.values() if v not in (None, ""))
    haystack = " ".join(haystack_parts).lower()

    dependencies: list[dict] = []
    seen: set[str] = set()

    for file_ref in _extract_known_file_refs(plan_outputs):
        candidates = [
            file_ref.get("workspace_path"),
            Path(file_ref.get("path") or "").name,
            file_ref.get("module_name"),
        ]
        if not any(candidate and str(candidate).lower() in haystack for candidate in candidates):
            continue
        dep_id = file_ref.get("file_id") or file_ref.get("abs_path")
        if not dep_id or dep_id in seen:
            continue
        seen.add(dep_id)
        dependencies.append({
            "dependency_type": "file",
            "file_id": file_ref.get("file_id"),
            "artifact_id": None,
            "task_index": file_ref.get("origin_task_index"),
            "path": file_ref.get("workspace_path") or file_ref.get("abs_path"),
            "module_name": file_ref.get("module_name"),
        })

    for artifact_ref in _extract_known_artifact_refs(plan_outputs):
        file_ref = artifact_ref.get("file_ref") if isinstance(artifact_ref.get("file_ref"), dict) else artifact_ref
        candidates = [
            artifact_ref.get("artifact_id"),
            file_ref.get("workspace_path") if isinstance(file_ref, dict) else None,
            Path(file_ref.get("path") or "").name if isinstance(file_ref, dict) and file_ref.get("path") else None,
        ]
        if not any(candidate and str(candidate).lower() in haystack for candidate in candidates):
            continue
        dep_id = artifact_ref.get("artifact_id")
        if not dep_id or dep_id in seen:
            continue
        seen.add(dep_id)
        dependencies.append({
            "dependency_type": "artifact",
            "file_id": file_ref.get("file_id") if isinstance(file_ref, dict) else None,
            "artifact_id": artifact_ref.get("artifact_id"),
            "task_index": file_ref.get("origin_task_index") if isinstance(file_ref, dict) else None,
            "path": file_ref.get("workspace_path") or file_ref.get("abs_path") if isinstance(file_ref, dict) else None,
            "module_name": file_ref.get("module_name") if isinstance(file_ref, dict) else None,
        })

    return dependencies


def _format_dependency_context(dependencies: list[dict]) -> str:
    """Render dependency links for translator/replan guidance."""
    if not dependencies:
        return ""
    lines = []
    for dep in dependencies:
        ref = dep.get("artifact_id") or dep.get("file_id") or dep.get("path")
        path = dep.get("path")
        module_name = dep.get("module_name")
        task_index = dep.get("task_index")
        bits = [str(ref)]
        if path and path != ref:
            bits.append(f"path={path}")
        if module_name:
            bits.append(f"module={module_name}")
        if task_index is not None:
            bits.append(f"from_task={task_index}")
        lines.append("- " + ", ".join(bits))
    return "## Authoritative Dependencies\n" + "\n".join(lines)


def _build_tool_file_refs(
    session: str,
    args: dict,
    *,
    task_index: int,
    tool_name: str | None,
) -> list[dict]:
    """Build canonical file refs for tool args that point at real files."""
    workspace = _session_workspace(session)
    refs: list[dict] = []
    seen: set[str] = set()
    for value in args.values():
        if not isinstance(value, str):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = workspace / value
        if not candidate.exists() or not candidate.is_file():
            continue
        file_ref = _make_file_ref(
            candidate,
            workspace=workspace,
            origin_task_index=task_index,
            origin_tool=tool_name,
        ).to_dict()
        if file_ref["file_id"] in seen:
            continue
        seen.add(file_ref["file_id"])
        refs.append(file_ref)
    return refs


def _build_new_artifact_refs(
    session: str,
    pre_snapshot: set[Path],
    *,
    task_index: int,
    tool_name: str | None,
) -> list[dict]:
    """Build artifact refs for newly created visible files since *pre_snapshot*."""
    workspace = _session_workspace(session)
    refs: list[dict] = []
    for path in sorted(set(workspace.rglob("*")) - pre_snapshot):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            continue
        parts = rel.parts
        if not parts or parts[0] == ".kiso" or parts[0] in _PUB_IGNORE_DIRS:
            continue
        if any(part.startswith(".") for part in parts):
            continue
        artifact = {
            **_make_file_ref(
                path,
                workspace=workspace,
                origin_task_index=task_index,
                origin_tool=tool_name,
            ).to_dict(),
            "artifact_id": f"artifact:{rel.as_posix()}",
            "artifact_kind": "file",
            "tool": tool_name,
        }
        refs.append(artifact)
    return refs


def _repair_exec_pythonpath(command: str, plan_outputs: list[dict]) -> str:
    """Inject PYTHONPATH for known imported modules outside the session workspace."""
    if "python" not in command:
        return command
    module_names = {m.group(1) for m in _PY_IMPORT_RE.finditer(command)}
    if not module_names:
        return command
    extra_dirs: list[str] = []
    for file_ref in _extract_known_file_refs(plan_outputs):
        module_name = file_ref.get("module_name")
        abs_path = file_ref.get("abs_path")
        if not module_name or module_name not in module_names or not abs_path:
            continue
        if file_ref.get("workspace_path"):
            continue
        parent = str(Path(abs_path).parent)
        if parent not in extra_dirs:
            extra_dirs.append(parent)
    if not extra_dirs:
        return command
    py_path = ":".join(extra_dirs)
    return f"PYTHONPATH='{py_path}':\"${{PYTHONPATH:-}}\" {command}"


def _looks_like_workspace_file_arg(arg_name: str, arg_schema: dict, value: object) -> bool:
    """Heuristic: True when a tool arg likely references a workspace file."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return False
    name_lower = (arg_name or "").lower()
    desc_lower = str((arg_schema or {}).get("description", "")).lower()
    if any(hint in name_lower for hint in _FILE_ARG_NAME_HINTS):
        return True
    if "workspace" in desc_lower or "file" in desc_lower or "path" in desc_lower:
        return True
    if any(ch in text for ch in _GLOB_CHARS):
        return True
    return "/" in text or "." in Path(text).name


def _normalize_stem_tokens(path_text: str) -> tuple[str, str]:
    """Return (normalized stem, suffix) for fuzzy file matching."""
    name = Path(path_text).name
    stem = Path(name).stem.lower()
    suffix = Path(name).suffix.lower()
    normalized = re.sub(r"[^a-z0-9]+", "", stem)
    return normalized, suffix


def _resolve_workspace_file_reference(session: str, value: str) -> str | None:
    """Resolve a missing workspace file reference to a unique existing file."""
    raw = (value or "").strip()
    if not raw or raw.startswith(("http://", "https://")):
        return None
    if raw.startswith("/"):
        return None

    workspace = _session_workspace(session)
    if (workspace / raw).is_file():
        return raw

    visible_files = _workspace_visible_files(session)
    if not visible_files:
        return None

    basename = Path(raw).name
    glob_like = any(ch in raw for ch in _GLOB_CHARS)

    if glob_like:
        matches = [
            rel for rel in visible_files
            if fnmatch.fnmatchcase(rel, raw) or fnmatch.fnmatchcase(Path(rel).name, basename)
        ]
        if len(matches) == 1:
            return matches[0]

    exact_name_matches = [rel for rel in visible_files if Path(rel).name == basename]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]

    wanted_norm, wanted_suffix = _normalize_stem_tokens(raw)
    if not wanted_norm and not wanted_suffix:
        return None

    fuzzy_matches: list[str] = []
    for rel in visible_files:
        rel_norm, rel_suffix = _normalize_stem_tokens(rel)
        if wanted_suffix and rel_suffix and rel_suffix != wanted_suffix:
            continue
        if wanted_norm and rel_norm and (wanted_norm in rel_norm or rel_norm in wanted_norm):
            fuzzy_matches.append(rel)
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]
    return None


def _repair_tool_workspace_args(tool_info: dict, args: dict, session: str) -> dict:
    """Repair missing local-file tool args when a unique workspace match exists."""
    corrected = dict(args)
    args_schema = tool_info.get("args_schema", {}) or {}
    for arg_name, value in list(corrected.items()):
        schema = args_schema.get(arg_name, {})
        if not _looks_like_workspace_file_arg(arg_name, schema, value):
            continue
        resolved = _resolve_workspace_file_reference(session, value)
        if resolved and resolved != value:
            corrected[arg_name] = resolved
    return corrected
