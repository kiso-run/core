"""Canonical worker runtime state helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(slots=True)
class FileRef:
    """Canonical file identity shared across worker, planner, and replans."""

    file_id: str
    abs_path: str
    workspace_path: str | None
    type: str
    exists: bool
    module_name: str | None = None
    origin_task_index: int | None = None
    origin_wrapper: str | None = None

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "path": self.workspace_path or self.abs_path,
            "workspace_path": self.workspace_path,
            "abs_path": self.abs_path,
            "type": self.type,
            "exists": self.exists,
            "module_name": self.module_name,
            "origin_task_index": self.origin_task_index,
            "origin_wrapper": self.origin_wrapper,
        }


@dataclass(slots=True)
class ArtifactRef:
    """Canonical artifact identity. First pass is file-backed only."""

    artifact_id: str
    kind: str
    file_ref: FileRef

    def to_dict(self) -> dict:
        data = self.file_ref.to_dict()
        data["artifact_id"] = self.artifact_id
        data["artifact_kind"] = self.kind
        return data


@dataclass(slots=True)
class TaskContract:
    """Normalized execution contract derived from a planner task or DB row."""

    task_type: str
    intent: str
    wrapper_name: str | None
    args: dict | None
    expect: str | None
    delivery_mode: str
    verification_mode: str
    allowed_repair_scope: str
    declared_inputs: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    task_index: int | None = None
    task_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "intent": self.intent,
            "wrapper_name": self.wrapper_name,
            "args": self.args,
            "expect": self.expect,
            "delivery_mode": self.delivery_mode,
            "verification_mode": self.verification_mode,
            "allowed_repair_scope": self.allowed_repair_scope,
            "declared_inputs": list(self.declared_inputs),
            "expected_outputs": list(self.expected_outputs),
            "dependencies": list(self.dependencies),
            "task_index": self.task_index,
            "task_id": self.task_id,
        }


def _coerce_task_args(args: object) -> dict | None:
    """Normalize planner/DB task args into a dict when possible."""
    if args is None:
        return None
    if isinstance(args, dict):
        return dict(args)
    if isinstance(args, str):
        text = args.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _coerce_task_contract(contract: object) -> TaskContract | None:
    """Normalize a stored/raw contract payload into ``TaskContract``."""
    if isinstance(contract, TaskContract):
        return contract
    if not isinstance(contract, dict):
        return None
    return TaskContract(
        task_type=str(contract.get("task_type") or ""),
        intent=str(contract.get("intent") or ""),
        wrapper_name=contract.get("wrapper_name"),
        args=_coerce_task_args(contract.get("args")),
        expect=contract.get("expect"),
        delivery_mode=str(contract.get("delivery_mode") or ""),
        verification_mode=str(contract.get("verification_mode") or ""),
        allowed_repair_scope=str(contract.get("allowed_repair_scope") or ""),
        declared_inputs=list(contract.get("declared_inputs") or []),
        expected_outputs=list(contract.get("expected_outputs") or []),
        dependencies=list(contract.get("dependencies") or []),
        task_index=contract.get("task_index"),
        task_id=contract.get("task_id"),
    )


def _normalize_task_contract(
    task: dict,
    *,
    task_index: int | None = None,
    task_id: int | None = None,
) -> TaskContract:
    """Derive a declarative contract from an existing planner task shape."""
    task_type = str(task.get("type") or "")
    intent = str(task.get("detail") or "")
    wrapper_name = task.get("tool") or task.get("skill")
    args = _coerce_task_args(task.get("args"))
    expect = task.get("expect")

    delivery_mode = "user-facing" if task_type == "msg" else "action"
    verification_mode = "none" if task_type in {"msg", "replan"} else "review"
    allowed_repair_scope = "plan" if task_type in {"msg", "replan"} else "task"

    declared_inputs: list[str] = []
    if wrapper_name:
        declared_inputs.append(f"tool:{wrapper_name}")
    if args:
        declared_inputs.extend(
            f"{name}={value}"
            for name, value in sorted(args.items())
            if value not in (None, "", [], {})
        )

    expected_outputs = [str(expect)] if expect else []

    return TaskContract(
        task_type=task_type,
        intent=intent,
        wrapper_name=wrapper_name,
        args=args,
        expect=expect,
        delivery_mode=delivery_mode,
        verification_mode=verification_mode,
        allowed_repair_scope=allowed_repair_scope,
        declared_inputs=declared_inputs,
        expected_outputs=expected_outputs,
        task_index=task_index if task_index is not None else task.get("index"),
        task_id=task_id if task_id is not None else task.get("id"),
    )


@dataclass(slots=True)
class TaskResult:
    """Canonical runtime result for a task across execution, replan, and delivery."""

    task_id: int | None
    task_index: int
    task_type: str
    detail: str
    status: str
    output: str
    stderr: str | None = None
    reviewer_summary: str | None = None
    retry_hint: str | None = None
    failure_class: str | None = None
    exit_code: int | None = None
    wrapper_name: str | None = None
    contract: TaskContract | None = None
    file_refs: list[dict] = field(default_factory=list)
    artifact_refs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "index": self.task_index,
            "type": self.task_type,
            "detail": self.detail,
            "status": self.status,
            "output": self.output,
            "stderr": self.stderr,
            "reviewer_summary": self.reviewer_summary,
            "retry_hint": self.retry_hint,
            "failure_class": self.failure_class,
            "exit_code": self.exit_code,
            "tool": self.wrapper_name,
            "contract": self.contract.to_dict() if self.contract else None,
            "file_refs": list(self.file_refs),
            "artifact_refs": list(self.artifact_refs),
        }


def _task_result_from_source(entry: dict) -> TaskResult:
    """Normalize a task row or plan_output dict into ``TaskResult``."""
    contract = _coerce_task_contract(entry.get("contract"))
    return TaskResult(
        task_id=entry.get("task_id") or entry.get("id"),
        task_index=int(entry.get("index") or 0),
        task_type=str(entry.get("type") or ""),
        detail=str(entry.get("detail") or ""),
        status=str(entry.get("status") or ""),
        output=str(entry.get("output") or ""),
        stderr=entry.get("stderr"),
        reviewer_summary=entry.get("reviewer_summary"),
        retry_hint=entry.get("retry_hint"),
        failure_class=entry.get("failure_class"),
        exit_code=entry.get("exit_code"),
        wrapper_name=entry.get("tool") or entry.get("skill"),
        contract=contract,
        file_refs=list(entry.get("file_refs") or []),
        artifact_refs=list(entry.get("artifact_refs") or []),
    )


def _collect_task_results(
    completed: list[dict] | None = None,
    plan_outputs: list[dict] | None = None,
) -> list[TaskResult]:
    """Merge completed task rows and plan outputs into canonical results."""
    results: dict[int, TaskResult] = {}

    for fallback_index, source in enumerate(completed or [], 1):
        merged_source = dict(source)
        if not isinstance(merged_source.get("index"), int):
            merged_source["index"] = fallback_index
        result = _task_result_from_source(merged_source)
        results[result.task_index] = result

    for source in plan_outputs or []:
        result = _task_result_from_source(source)
        if result.task_index <= 0:
            continue
        existing = results.get(result.task_index)
        if existing is None:
            results[result.task_index] = result
            continue
        if result.contract is not None:
            existing.contract = result.contract
        if result.file_refs:
            existing.file_refs = result.file_refs
        if result.artifact_refs:
            existing.artifact_refs = result.artifact_refs
        if result.reviewer_summary:
            existing.reviewer_summary = result.reviewer_summary
        if result.retry_hint:
            existing.retry_hint = result.retry_hint
        if result.failure_class:
            existing.failure_class = result.failure_class
        if result.output:
            existing.output = result.output
        if result.stderr:
            existing.stderr = result.stderr
        if result.status:
            existing.status = result.status

    return [results[idx] for idx in sorted(results)]
