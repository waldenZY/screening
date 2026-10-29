"""Optional Stage2 annotations; no admission or core delivery responsibilities."""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
from ..stage1 import QualityResponseError, stable_hash
from ...shared.json_io import JsonObject, load_jsonl, write_json, write_jsonl, sha256_file

STAGE2_FIELDS = frozenset({"task_difficulty", "execution_issues"})
EXECUTION_ISSUES = frozenset({"no_evaluable_goal", "attachment_dependency", "unresolved_reference", "other_unfit"})
TASK_DIFFICULTIES = frozenset({"atomic", "simple", "moderate", "complex", "extreme", "not_applicable"})

def stage2_semantic_review(results: Sequence[Mapping[str, Any]]) -> JsonObject:
    """Summarize final-member issues without implying human-gold accuracy."""
    validated = [validate_stage2_result(dict(value)) for value in results]
    counts = Counter(issue for value in validated for issue in value["execution_issues"])
    with_issues = sum(bool(value["execution_issues"]) for value in validated)
    return {
        "notice": "Stage2 语义问题复核信号；不是人工真值准确率。分类计数允许重叠。",
        "evaluated_final": len(validated),
        "with_issues": with_issues,
        "by_issue": {key: counts[key] for key in sorted(EXECUTION_ISSUES)},
        "rate": None if not validated else round(with_issues / len(validated), 6),
    }


def validate_stage2_result(value: Any) -> JsonObject:
    if not isinstance(value, dict) or set(value) != STAGE2_FIELDS:
        raise QualityResponseError("第二阶段响应字段必须与白名单完全一致")
    task = value.get("task_difficulty")
    execution_issues = value.get("execution_issues")
    if not isinstance(task, str) or task not in TASK_DIFFICULTIES:
        raise QualityResponseError("第二阶段 task_difficulty 枚举非法")
    if (not isinstance(execution_issues, list)
            or any(not isinstance(item, str) or item not in EXECUTION_ISSUES for item in execution_issues)
            or len(set(execution_issues)) != len(execution_issues)):
        raise QualityResponseError("第二阶段 execution_issues 非法")
    no_goal = "no_evaluable_goal" in execution_issues
    if (task == "not_applicable") != no_goal:
        raise QualityResponseError("not_applicable 必须与 no_evaluable_goal 同时出现")
    if no_goal and execution_issues != ["no_evaluable_goal"]:
        raise QualityResponseError("no_evaluable_goal 不得与其他 execution_issues 并列")
    return {
        "task_difficulty": task,
        "execution_issues": list(execution_issues),
    }



def stage_definition(prompt: str):
    from ..runner import AdditionalQualityStage, conversation_quality_input
    return AdditionalQualityStage(prompt, conversation_quality_input, validate_stage2_result)


def _report(values):
    return {"module": "optional_stage2", "status": "completed",
            "semantic_review": stage2_semantic_review(values),
            "task_difficulty": {k: sum(v["task_difficulty"] == k for v in values) for k in sorted(TASK_DIFFICULTIES)}}


def annotate_branch(runner, candidates: Sequence[JsonObject], branch_root: Path, branch: str) -> tuple[str, ...]:
    results = runner.run_additional_stage("stage2", candidates, branch=branch) if candidates else {}
    rows = [{"source_identity_sha256": c["source_identity_sha256"],
             "task_id": runner.task_id("stage2", c), "result": results[c["source_identity_sha256"]]} for c in candidates]
    folder = branch_root / "optional_annotations"
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / "stage2.jsonl", rows)
    write_json(folder / "stage2_report.json", _report(list(results.values())))
    return ("optional_annotations/stage2.jsonl", "optional_annotations/stage2_report.json")


def validate_branch(run_root: Path, branch: str, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Bind sidecars to their own model tasks without altering core records."""
    from ..runner import conversation_quality_input
    quality_root = run_root / "quality"
    contract = json.loads((quality_root / "quality_run_contract.json").read_text(encoding="utf-8"))
    prompt_path = quality_root / "stage2_prompt.md"
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    if (stable_hash(prompt_path.read_text(encoding="utf-8")) != contract["prompt_hashes"]["stage2"]
            or sha256_file(prompt_path) != manifest["contract"]["prompts"]["stage2_sha256"]):
        raise QualityResponseError("可选 Stage2 Prompt 与冻结契约不一致")
    task_rows = load_jsonl(quality_root / "stage2_tasks.jsonl")
    result_rows = load_jsonl(quality_root / "stage2_results.jsonl")
    tasks = {r.get("cache_key"): r for r in task_rows}
    results = {r.get("cache_key"): validate_stage2_result(r.get("result")) for r in result_rows}
    if (None in tasks or None in results or len(tasks) != len(task_rows) or len(results) != len(result_rows)
            or any(r.get("stage") != "stage2" for r in [*task_rows, *result_rows])):
        raise QualityResponseError("可选 Stage2 任务或结果账本非法")
    folder = run_root / branch / "optional_annotations"
    rows = load_jsonl(folder / "stage2.jsonl")
    if len(rows) != len(records):
        raise QualityResponseError("可选 Stage2 标注数量不一致")
    values = []
    for row, record in zip(rows, records):
        candidate = record["candidate"]
        identity = candidate["source_identity_sha256"]
        input_hash = stable_hash(conversation_quality_input(candidate))
        key = stable_hash({"stage": "stage2", "prompt": contract["prompt_hashes"]["stage2"],
                           "options": contract["options_sha256"], "input_sha256": input_hash})
        task = tasks.get(key)
        if (set(row) != {"source_identity_sha256", "task_id", "result"} or row["task_id"] != key
                or row["source_identity_sha256"] != identity or task is None or key not in results
                or task.get("input_sha256") != input_hash or task.get("prompt_sha256") != contract["prompt_hashes"]["stage2"]
                or task.get("options_sha256") != contract["options_sha256"]
                or identity not in task.get("source_identities", [])
                or candidate["source"]["source_line"] not in task.get("source_lines", [])
                or validate_stage2_result(row["result"]) != results[key]):
            raise QualityResponseError("可选 Stage2 标注与会话、任务或结果不一致")
        values.append(results[key])
    if json.loads((folder / "stage2_report.json").read_text(encoding="utf-8")) != _report(values):
        raise QualityResponseError("可选 Stage2 汇总与标注不一致")
    return {name: {"path": path.relative_to(run_root).as_posix(), "sha256": sha256_file(path)}
            for name, path in (("results", folder / "stage2.jsonl"), ("report", folder / "stage2_report.json"))}
