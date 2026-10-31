"""Stage2 downstream quality annotation; never changes frozen membership."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
import json

from .admission import QualityResponseError, stable_hash, validate_admission_result
from ..shared.json_io import JsonObject, load_jsonl, write_json, write_jsonl, sha256_file

QUALITY_RUBRIC_VERSION = 1
QUALITY_FIELDS = (
    "quality_validity", "quality_intent_clarity", "quality_overall_score", "quality_issue_types",
)
ISSUE_TYPES = (
    "none", "category_mismatch", "ambiguous_intent", "context_dependent", "truncated",
    "nonsense", "emotional_only", "too_brief", "no_evaluable_goal", "multi_intent",
)
VALIDITIES = frozenset({"valid", "borderline", "invalid"})
CLARITIES = frozenset({"clear", "vague", "no_intent"})


def validate_quality_annotation(value: Any) -> JsonObject:
    if not isinstance(value, dict) or set(value) != set(QUALITY_FIELDS):
        raise QualityResponseError("下游质量标注必须且只能包含四个 quality_* 字段")
    validity, clarity = value["quality_validity"], value["quality_intent_clarity"]
    score, issues = value["quality_overall_score"], value["quality_issue_types"]
    if not isinstance(validity, str) or validity not in VALIDITIES:
        raise QualityResponseError("quality_validity 枚举非法")
    if not isinstance(clarity, str) or clarity not in CLARITIES:
        raise QualityResponseError("quality_intent_clarity 枚举非法")
    if type(score) is not int or not 1 <= score <= 5:
        raise QualityResponseError("quality_overall_score 必须为1～5整数")
    if (not isinstance(issues, list) or not issues
            or any(not isinstance(x, str) or x not in ISSUE_TYPES for x in issues)
            or len(issues) != len(set(issues)) or ("none" in issues and issues != ["none"])):
        raise QualityResponseError("quality_issue_types 必须为无重复非空枚举数组，none只能单独使用")
    if score == 5:
        if validity != "valid" or clarity != "clear" or issues != ["none"]:
            raise QualityResponseError("5分要求 valid、clear、[none]")
    elif issues == ["none"]:
        raise QualityResponseError("低于5分必须给出具体质量问题")
    if score == 4 and (validity != "valid" or clarity == "no_intent"):
        raise QualityResponseError("4分要求有效且仍有可识别目标")
    if score == 3 and (validity != "borderline" or clarity == "no_intent"):
        raise QualityResponseError("3分要求 borderline 且目标仍可识别")
    if score == 2 and (validity != "invalid" or clarity == "no_intent"):
        raise QualityResponseError("2分要求 invalid 且目标仍可识别")
    if ((score == 1) != (clarity == "no_intent")
            or (score == 1 and (validity != "invalid" or not set(issues).intersection(
                {"nonsense", "emotional_only", "too_brief", "no_evaluable_goal"})))):
        raise QualityResponseError("1分要求 invalid、no_intent 及有证据的无目标问题")
    if "no_evaluable_goal" in issues and score != 1:
        raise QualityResponseError("no_evaluable_goal 必须为1分")
    return {key: list(value[key]) if key == "quality_issue_types" else value[key] for key in QUALITY_FIELDS}


def quality_annotation_input(candidate: Mapping[str, Any], admission: Mapping[str, Any]) -> JsonObject:
    from .runner import conversation_quality_input
    return {
        **conversation_quality_input(candidate),
        "candidate_category": {"L1": candidate["taxonomy"]["l1"], "L2": candidate["taxonomy"]["l2"]},
        "admission": validate_admission_result(dict(admission))["admission"],
        "quality_rubric_version": QUALITY_RUBRIC_VERSION,
    }


def stage_definition(prompt: str):
    from .runner import AdditionalQualityStage
    return AdditionalQualityStage(prompt, quality_annotation_input, validate_quality_annotation)


def quality_report(rows: Sequence[Mapping[str, Any]]) -> JsonObject:
    values = [validate_quality_annotation(dict(row["result"])) for row in rows]
    return {
        "quality_rubric_version": QUALITY_RUBRIC_VERSION,
        "evaluated": len(values),
        "score_distribution": {str(score): sum(v["quality_overall_score"] == score for v in values) for score in range(1, 6)},
        "validity_distribution": dict(sorted(Counter(v["quality_validity"] for v in values).items())),
        "clarity_distribution": dict(sorted(Counter(v["quality_intent_clarity"] for v in values).items())),
        "issue_distribution": dict(sorted(Counter(x for v in values for x in v["quality_issue_types"]).items())),
        "review_disagreements": [
            {"source_identity_sha256": row["source_identity_sha256"], "task_id": row["task_id"], "result": row["result"]}
            for row in rows if row["result"]["quality_overall_score"] < 5
        ],
        "notice": "低分及问题是补充复核信号，不修改准入结论、成员、intent_label或executable，不代表人工真值准确率。",
    }


def annotate_branch(runner, candidates: Sequence[JsonObject], branch_root: Path, branch: str):
    results = runner.run_additional_stage("quality_annotation", candidates, branch=branch) if candidates else {}
    rows = [{"source_identity_sha256": c["source_identity_sha256"],
             "task_id": runner.task_id("quality_annotation", c), "result": results[c["source_identity_sha256"]]}
            for c in candidates]
    folder = branch_root / "stage2"
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / "quality_annotations.jsonl", rows)
    write_json(folder / "quality_report.json", quality_report(rows))
    return results, ("stage2/quality_annotations.jsonl", "stage2/quality_report.json")


def validate_branch(run_root: Path, branch: str, records, manifest, contract) -> JsonObject:
    """Rebuild task keys from the complete frozen admission input and check sidecars."""
    quality_root = run_root / "quality"
    prompt = quality_root / "quality_annotation_prompt.md"
    if (contract.get("additional_stages") != ["quality_annotation"]
            or stable_hash(prompt.read_text(encoding="utf-8")) != contract["prompt_hashes"]["quality_annotation"]
            or sha256_file(prompt) != manifest["contract"]["prompts"]["quality_annotation_sha256"]):
        raise QualityResponseError("下游质量标注 Prompt 与冻结契约不一致")
    task_rows = load_jsonl(quality_root / "quality_annotation_tasks.jsonl")
    result_rows = load_jsonl(quality_root / "quality_annotation_results.jsonl")
    tasks = {r.get("cache_key"): r for r in task_rows}
    results = {r.get("cache_key"): validate_quality_annotation(r.get("result")) for r in result_rows}
    if (None in tasks or None in results or len(tasks) != len(task_rows) or len(results) != len(result_rows)
            or any(r.get("stage") != "quality_annotation" for r in [*task_rows, *result_rows])):
        raise QualityResponseError("下游质量标注账本非法")
    folder = run_root / branch / "stage2"
    rows = load_jsonl(folder / "quality_annotations.jsonl")
    frozen = load_jsonl(run_root / branch / "stage1.frozen.jsonl")
    from ..delivery.screened import frozen_stage1_record
    freeze_report = json.loads((run_root / branch / "stage1_freeze_report.json").read_text(encoding="utf-8"))
    if (len(rows) != len(records) or frozen != [frozen_stage1_record(r) for r in records]
            or freeze_report != {"stage": "stage1", "records": len(frozen), "sha256": sha256_file(run_root / branch / "stage1.frozen.jsonl")}):
        raise QualityResponseError("Stage1 冻结成员与交付记录不一致")
    for row, record in zip(rows, records):
        candidate = record["candidate"]
        identity = candidate["source_identity_sha256"]
        payload = quality_annotation_input(candidate, {"admission": record["quality"]["admission"]})
        input_hash = stable_hash(payload)
        key = stable_hash({"stage": "quality_annotation", "prompt": contract["prompt_hashes"]["quality_annotation"],
                           "options": contract["options_sha256"], "input_sha256": input_hash})
        task = tasks.get(key)
        if (set(row) != {"source_identity_sha256", "task_id", "result"} or row["task_id"] != key
                or row["source_identity_sha256"] != identity or task is None or key not in results
                or task.get("input_sha256") != input_hash or task.get("prompt_sha256") != contract["prompt_hashes"]["quality_annotation"]
                or task.get("options_sha256") != contract["options_sha256"]
                or identity not in task.get("source_identities", [])
                or candidate["source"]["source_line"] not in task.get("source_lines", [])
                or validate_quality_annotation(row["result"]) != results[key]
                or record["downstream_quality"] != results[key]
                or record["provenance"]["quality_annotation_task_id"] != key):
            raise QualityResponseError("下游质量标注与成员、任务或结果账本不一致")
    if json.loads((folder / "quality_report.json").read_text(encoding="utf-8")) != quality_report(rows):
        raise QualityResponseError("下游质量标注报告与逐条结果不一致")
    paths = {"results": folder / "quality_annotations.jsonl", "report": folder / "quality_report.json",
             "frozen": run_root / branch / "stage1.frozen.jsonl", "freeze_report": run_root / branch / "stage1_freeze_report.json"}
    return {name: {"path": path.relative_to(run_root).as_posix(), "sha256": sha256_file(path)} for name, path in paths.items()}
