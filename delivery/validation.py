"""Independent internal and external artifact validation."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..quality.admission import admission_usable, category_fit, validate_features, validate_category, build_admission_result
from ..candidate.index import (
    CanonicalIndexHandle,
    validate_canonical_database,
    validate_canonical_source,
)
from ..candidate.p2_adaptive import P2_ALGORITHM, P2_SOURCE_NAMES
from ..candidate.dedup import RULES_SHA256, normalize_text, selection_text, normalize_unit_similarity
from ..pipeline.orchestrator import BUILD_CONFIG_VERSION
from ..pipeline.run_context import PIPELINE_VERSION
from ..shared.json_io import JsonObject, iter_jsonl, load_jsonl, sha256_file, stable_hash
from ..pipeline.layout import external_root, prompt_path
from .labeled import (
    LABELED_FILENAMES,
    base_filename, base_fields,
    LABELED_MAPPING_VERSION,
    labeled_fields,
    validate_labeled_record,
)
from .eligibility import delivery_labels
from ..shared.risk_keywords import validate_risk_snapshot
from .report import (
    RUN_REPORT_SCHEMA_VERSION,
    RUN_REPORT_JSON,
    RUN_REPORT_MARKDOWN,
    RunReportError,
    load_and_validate_run_report,
)
from .screened import (
    SCREENED_CANONICAL_SCHEMA_VERSION,
    stable_record_sha256,
    validate_screened_canonical_records,
)
from ..quality.runner import (CHALLENGE_FIELDS, CHALLENGE_TYPES, QUALITY_CONTRACT_VERSION, admission_quality_input, admission_input_sha256, admission_task_key, validate_challenge_result)
from ..quality.admission import (ADMISSION_FIELDS, admission_passes, admission_issue_summary, validate_admission_result)


class ArtifactValidationError(ValueError):
    """A v4 artifact or artifact contract violation."""


def _validate_dedup_audit_row(
    set_name: str, path: Path, line_no: int, row: JsonObject, rules: Mapping[str, Any],
) -> JsonObject:
    """Validate an in-memory copy, preserving historical audit bytes and hashes."""
    metrics = ("candidate_jaccard", "weighted_jaccard", "edit_similarity")
    labels = {"drop", "retain", "exact_normalized", "near_duplicate", "below_confirmation_threshold"}

    def fail(fields: tuple[str, ...], message: str = "invalid dedup audit row") -> None:
        values = {}
        for field in fields:
            value = row.get(field)
            if isinstance(value, (int, float)) or (isinstance(value, str) and value in labels):
                values[field] = repr(value)
            else:
                values[field] = f"<{type(value).__name__}>"
        details = {"file": str(path), "line": line_no, "fields": list(fields), "values": values}
        raise ArtifactValidationError(f"{set_name} {message}: " + json.dumps(details, ensure_ascii=False, sort_keys=True))

    if not isinstance(row.get("decision"), str) or row["decision"] not in {"drop", "retain"}:
        fail(("decision",))
    if not isinstance(row.get("reason"), str) or row["reason"] not in {
        "exact_normalized", "near_duplicate", "below_confirmation_threshold"
    }:
        fail(("reason",))
    for field in ("candidate", "representative"):
        if not isinstance(row.get(field), str):
            fail((field,))
    checked = dict(row)
    for field in metrics:
        try:
            checked[field] = normalize_unit_similarity(row.get(field))
        except ValueError:
            fail((field,))
    reason = checked["reason"]
    if reason == "near_duplicate":
        if checked["decision"] != "drop":
            fail(("decision",), "invalid near duplicate decision")
        for field, threshold in zip(metrics, (
            "candidate_jaccard_min", "weighted_jaccard_min", "edit_similarity_min"
        )):
            if checked[field] < rules[threshold]:
                fail((field,), "invalid near duplicate decision")
    elif reason == "exact_normalized":
        if checked["decision"] != "drop":
            fail(("decision",), "invalid exact duplicate decision")
        for field in metrics:
            if checked[field] != 1.0:
                fail((field,), "invalid exact duplicate decision")
    else:
        if checked["decision"] != "retain":
            fail(("decision",), "invalid borderline decision")
        if checked["candidate_jaccard"] < rules["candidate_jaccard_min"]:
            fail(("candidate_jaccard",), "invalid borderline decision")
        if (checked["weighted_jaccard"] >= rules["weighted_jaccard_min"]
                and checked["edit_similarity"] >= rules["edit_similarity_min"]):
            fail(("weighted_jaccard", "edit_similarity"), "invalid borderline decision")
    return checked


def _require_current_quality_contract(manifest: Mapping[str, Any]) -> None:
    contract = manifest.get("contract")
    if (not isinstance(contract, Mapping)
            or contract.get("p012_build_config_version") != BUILD_CONFIG_VERSION
            or contract.get("screened_canonical_schema_version") != SCREENED_CANONICAL_SCHEMA_VERSION
            or contract.get("labeled_mapping_version") != LABELED_MAPPING_VERSION
            or contract.get("dedup_rules_sha256") != RULES_SHA256
            or not isinstance(contract.get("dedup_idf_sha256"), str)):
        raise ArtifactValidationError("Old dedup or mapping contract cannot be delivered")
    if not isinstance(contract, Mapping) or contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION:
        raise ArtifactValidationError("质量契约版本不一致，旧 run 必须重新运行")


def _load_object(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"无法读取运行元数据: {path.name}") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"运行元数据必须是对象: {path.name}")
    return value


def _jsonl_metadata(path: Path, run_root: Path, records: list[JsonObject]) -> JsonObject:
    return {
        "path": os.path.relpath(path, run_root).replace("\\", "/"),
        "sha256": sha256_file(path),
        "records": len(records),
    }


def _quality_annotations(run_root: Path, set_name: str, records, manifest, quality_contract) -> JsonObject:
    from ..quality.annotation import validate_branch
    try:
        if any(record["provenance"]["run_id"] != manifest.get("stage1_origin_run_id", manifest["run_id"]) for record in records):
            raise ValueError("provenance run_id 与本次运行不一致")
        return validate_branch(run_root, set_name, records, manifest, quality_contract)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ArtifactValidationError("Stage2 下游质量标注或 Stage1 冻结产物复验失败") from exc


def _task_maps(
    run_root: Path,
) -> tuple[dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject]]:
    quality_root = run_root / "quality"
    admission_task_rows = load_jsonl(quality_root / "admission_tasks.jsonl")
    challenge_task_rows = load_jsonl(quality_root / "challenge_tasks.jsonl")
    admission_result_rows = load_jsonl(quality_root / "admission_results.jsonl")
    challenge_result_rows = load_jsonl(quality_root / "challenge_results.jsonl")
    admission_tasks = {row.get("cache_key"): row for row in admission_task_rows}
    challenge_tasks = {row.get("cache_key"): row for row in challenge_task_rows}
    admission_results = {row.get("cache_key"): row.get("result") for row in admission_result_rows}
    challenge_results = {row.get("cache_key"): row.get("result") for row in challenge_result_rows}
    if None in admission_tasks or len(admission_tasks) != len(admission_task_rows):
        raise ArtifactValidationError("准入评估任务账本存在重复 ID")
    if None in challenge_tasks or len(challenge_tasks) != len(challenge_task_rows):
        raise ArtifactValidationError("Challenge 任务账本存在重复 ID")
    if None in admission_results or len(admission_results) != len(admission_result_rows):
        raise ArtifactValidationError("准入评估结果账本存在非法或重复 ID")
    if None in challenge_results or len(challenge_results) != len(challenge_result_rows):
        raise ArtifactValidationError("Challenge 结果账本存在非法或重复 ID")
    if any(row.get("stage") != "admission" for row in [*admission_task_rows, *admission_result_rows]):
        raise ArtifactValidationError("准入评估账本 stage 标记非法")
    if any(row.get("stage") != "challenge" for row in [*challenge_task_rows, *challenge_result_rows]):
        raise ArtifactValidationError("Challenge 账本 stage 标记非法")
    for stage, task_map in (
        ("admission", admission_tasks),
        ("challenge", challenge_tasks),
    ):
        cache_hit_rows = load_jsonl(quality_root / f"{stage}_cache_hits.jsonl")
        cache_hit_ids = {row.get("event_id") for row in cache_hit_rows}
        if None in cache_hit_ids or len(cache_hit_ids) != len(cache_hit_rows):
            raise ArtifactValidationError(f"{stage} 缓存命中账本存在非法或重复 ID")
        for row in cache_hit_rows:
            task = task_map.get(row.get("cache_key"))
            identities = row.get("source_identities")
            if (
                set(row) != {"event_id", "cache_key", "stage", "source_identities"}
                or row.get("stage") != stage
                or task is None
                or not isinstance(identities, list)
                or not identities
                or identities != sorted(set(identities))
                or not set(identities).issubset(set(task.get("source_identities", [])))
            ):
                raise ArtifactValidationError(f"{stage} 缓存命中账本内容非法")
    for result in admission_results.values():
        validate_admission_result(result)
    for result in challenge_results.values():
        validate_challenge_result(result)
    return admission_tasks, challenge_tasks, admission_results, challenge_results


def _validate_admission_components(quality_root: Path, contract: Mapping[str, Any], tasks: Mapping[str, JsonObject], results: Mapping[str, JsonObject], *, require_complete: bool = True, prompt_root: Path | None = None) -> None:
    """Recompute aggregate admission from independently recorded provider results."""
    hashes = contract.get("prompt_hashes", {})
    options = contract.get("options_sha256")
    policy = contract.get("screening_policy")
    expected_hash = stable_hash({"features": hashes.get("admission_features"), "category": hashes.get("admission_category"), "policy": policy})
    if hashes.get("admission") != expected_hash or set(tasks) != set(results):
        raise ArtifactValidationError("准入评估 聚合契约或终态不一致")
    components = {}
    for stage in ("admission_features", "admission_category"):
        task_rows = load_jsonl(quality_root / f"{stage}_tasks.jsonl")
        result_rows = load_jsonl(quality_root / f"{stage}_results.jsonl")
        tm = {r.get("cache_key"): r for r in task_rows}
        rm = {r.get("cache_key"): r.get("result") for r in result_rows}
        if None in tm or None in rm or len(tm) != len(task_rows) or len(rm) != len(result_rows):
            raise ArtifactValidationError("准入评估 子任务账本不一致")
        if not require_complete:
            field = "feature_task_id" if stage == "admission_features" else "category_task_id"
            required = {task.get(field) for task in tasks.values()}
            tm = {key: value for key, value in tm.items() if key in required}
            rm = {key: value for key, value in rm.items() if key in required}
            if set(tm) != required:
                raise ArtifactValidationError("分支缺少 准入评估 子任务")
        if set(tm) != set(rm):
            raise ArtifactValidationError("准入评估 子任务缺少合法终态")
        snapshot = (prompt_root / ("admission.md" if stage == "admission_features" else "category.md")) if prompt_root else prompt_path(quality_root.parent, stage)
        if not snapshot.is_file() or stable_hash(snapshot.read_text(encoding="utf-8")) != hashes.get(stage):
            raise ArtifactValidationError("准入评估 子任务 Prompt 不一致")
        if any(r.get("stage") != stage for r in result_rows):
            raise ArtifactValidationError("准入评估 子任务结果 stage 非法")
        for key in rm:
            task = tm[key]
            expected = stable_hash({"stage": stage, "prompt": hashes[stage], "options": options, "input_sha256": task.get("input_sha256")})
            if (key != expected or task.get("stage") != stage or task.get("prompt_sha256") != hashes[stage]
                    or task.get("options_sha256") != options):
                raise ArtifactValidationError("准入评估 子任务身份不一致")
            (validate_features if stage == "admission_features" else validate_category)(rm[key])
        components[stage] = (tm, rm)
    for key, task in tasks.items():
        ids = task.get("source_identities", [])
        linked = []
        for stage, field in (("admission_features", "feature_task_id"), ("admission_category", "category_task_id")):
            tm, rm = components[stage]
            component_id = task.get(field)
            if (component_id not in tm or component_id not in rm or not ids
                    or not set(ids).issubset(set(tm[component_id].get("source_identities", [])))):
                raise ArtifactValidationError("准入评估 子任务关联不一致")
            linked.append(rm[component_id])
        expected = build_admission_result(linked[0], linked[1], policy)
        if results[key] != expected or task.get("policy_sha256") != stable_hash(policy):
            raise ArtifactValidationError("准入评估 结果不能由子任务和冻结策略重建")


def _validate_challenge_binding(record: Mapping[str, Any], tasks: Mapping[str, JsonObject], results: Mapping[str, JsonObject], contract: Mapping[str, Any]) -> None:
    candidate = record["candidate"]
    challenge = record["challenge"]
    task_id = challenge["challenge_task_id"]
    task = tasks.get(task_id)
    hashes = contract["prompt_hashes"]
    options = contract["options_sha256"]
    from ..quality.runner import challenge_quality_input
    features = record["quality"]["admission"]["features"]
    expected_id = stable_hash({"stage": "challenge", "prompt": hashes["challenge"], "options": options,
        "conversation_sha256": candidate["conversation_sha256"], "l1": candidate["taxonomy"]["l1"],
        "l2": candidate["taxonomy"]["l2"], "admission_features": features})
    payload = challenge_quality_input(candidate, features)
    if (task_id != expected_id or not isinstance(task, Mapping) or task.get("stage") != "challenge"
            or task.get("prompt_sha256") != hashes["challenge"] or task.get("options_sha256") != options
            or task.get("input_sha256") != stable_hash(payload)
            or candidate["source_identity_sha256"] not in task.get("source_identities", [])
            or task_id not in results or validate_challenge_result(results[task_id]) != {key: challenge[key] for key in CHALLENGE_FIELDS}):
        raise ArtifactValidationError("Challenge 与输入特征或结果账本不一致")


def _validate_admission_binding(
    record: Mapping[str, Any],
    tasks: Mapping[str, JsonObject],
    results: Mapping[str, JsonObject],
    quality_contract: Mapping[str, Any],
) -> None:
    """Verify admitted members against the actual 准入评估 payload and ledger."""
    candidate = record["candidate"]
    task_id = record["selection"]["admission_task_id"]
    task = tasks.get(task_id)
    prompt_hashes = quality_contract.get("prompt_hashes")
    options_hash = quality_contract.get("options_sha256")
    if (quality_contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION
            or not isinstance(prompt_hashes, Mapping)
            or not isinstance(prompt_hashes.get("admission"), str)
            or not isinstance(options_hash, str)):
        raise ArtifactValidationError("准入评估 质量运行契约非法")
    prompt_hash = prompt_hashes["admission"]
    if (task_id != admission_task_key(candidate, prompt_sha256=prompt_hash, options_sha256=options_hash)
            or not isinstance(task, Mapping) or task.get("stage") != "admission"
            or task.get("input_sha256") != admission_input_sha256(candidate)
            or task.get("candidate_category_sha256") != stable_hash(admission_quality_input(candidate)["candidate_category"])
            or task.get("prompt_sha256") != prompt_hash
            or task.get("options_sha256") != options_hash or "taxonomy_sha256" in task
            or not isinstance(task.get("source_identities"), list)
            or not isinstance(task.get("source_lines"), list)
            or candidate["source_identity_sha256"] not in task.get("source_identities", [])
            or candidate["source"]["source_line"] not in task.get("source_lines", [])
            or task_id not in results):
        raise ArtifactValidationError("准入评估 输入哈希或任务关联不一致")
    result = validate_admission_result(results[task_id])
    feature_input = {key: admission_quality_input(candidate)[key] for key in ("sample_type", "conversation_turns")}
    for stage, payload, field in (("admission_features", feature_input, "feature_task_id"), ("admission_category", admission_quality_input(candidate), "category_task_id")):
        expected_component = stable_hash({"stage": stage, "prompt": prompt_hashes[stage], "options": options_hash, "input_sha256": stable_hash(payload)})
        if task.get(field) != expected_component:
            raise ArtifactValidationError("准入评估 子任务与实际输入不一致")
        selection_field = "admission_feature_task_id" if stage == "admission_features" else "admission_category_task_id"
        if record["selection"].get(selection_field) != expected_component:
            raise ArtifactValidationError("透传的准入子任务ID与实际输入不一致")
    if not admission_passes(result) or result != {key: record["quality"][key] for key in ADMISSION_FIELDS}:
        raise ArtifactValidationError("准入评估 准入标签与结果账本不一致")


def _collect_and_validate_artifacts(run_root: Path) -> JsonObject:
    """Independently validate every authoritative and compatibility artifact."""
    run_root = run_root.expanduser().resolve()
    manifest = _load_object(run_root / "manifest.json")
    _require_current_quality_contract(manifest)
    snapshot = _load_object(run_root / "snapshots/config.json")
    if snapshot.get("contract") != manifest.get("contract"):
        raise ArtifactValidationError("有效配置快照与 manifest 契约不一致")
    idf_snapshot = _load_object(run_root / "snapshots/dedup_idf.json")
    idf_content = {key: value for key, value in idf_snapshot.items() if key != "content_sha256"}
    if (idf_snapshot.get("rules_sha256") != RULES_SHA256
            or idf_snapshot.get("source_snapshot_id") != manifest.get("source", {}).get("sha256")
            or stable_hash(idf_content) != idf_snapshot.get("content_sha256")
            or idf_snapshot.get("content_sha256") != manifest["contract"].get("dedup_idf_sha256")):
        raise ArtifactValidationError("Frozen dedup IDF snapshot mismatch")
    risk_snapshot = _load_object(run_root / "snapshots/risk_keywords.json")
    validate_risk_snapshot(risk_snapshot)
    if (risk_snapshot["source_sha256"] != manifest.get("contract", {}).get("risk_keywords_source_sha256")
            or risk_snapshot["rules_sha256"] != manifest.get("contract", {}).get("risk_keywords_rules_sha256")):
        raise ArtifactValidationError("风险词表快照与 run 契约不一致")
    summary = _load_object(run_root / RUN_REPORT_JSON)["build_summary"]
    aggregate_export = {"labeled_mapping_version": LABELED_MAPPING_VERSION, "sets": manifest.get("exports", {}), "total_records": sum(r["records"] for r in manifest.get("exports", {}).values()), "lossy_enum_mappings": 0}
    if aggregate_export.get("labeled_mapping_version") != LABELED_MAPPING_VERSION:
        raise ArtifactValidationError("labeled mapping version 不一致")
    admission_tasks, challenge_tasks, admission_results, challenge_results = _task_maps(run_root)
    quality_contract = _load_object(run_root / "snapshots/quality_contract.json")
    if quality_contract.get("screening_policy") != manifest["contract"].get("screening_policy"):
        raise ArtifactValidationError("筛选策略与 manifest 不一致")
    origin = run_root / "snapshots/stage1_quality_contract.json"
    stage1_contract = _load_object(origin) if origin.exists() else quality_contract
    if origin.exists() and stable_hash(stage1_contract) != manifest.get("stage1_source", {}).get("quality_contract_sha256"):
        raise ArtifactValidationError("Stage1 独立来源契约不一致")
    _validate_admission_components(run_root / "quality", stage1_contract, admission_tasks, admission_results)
    admission_by_identity: dict[str, JsonObject] = {}
    for task_id, task in admission_tasks.items():
        result = admission_results.get(task_id)
        if result is None:
            raise ArtifactValidationError("准入评估任务缺少合法终态")
        validated = validate_admission_result(result)
        for identity in task.get("source_identities", []):
            previous = admission_by_identity.get(str(identity))
            if previous is not None and previous != validated:
                raise ArtifactValidationError("同一源 identity 对应多个准入评估结果")
            admission_by_identity[str(identity)] = validated
    if set(challenge_results) != set(challenge_tasks):
        raise ArtifactValidationError("Challenge 任务与结果账本不一致")
    challenge_by_identity: dict[str, JsonObject] = {}
    for task_id, task in challenge_tasks.items():
        validated = validate_challenge_result(challenge_results[task_id])
        identities = [str(value) for value in task.get("source_identities", [])]
        if not identities or any(
            identity not in admission_by_identity or not admission_passes(admission_by_identity[identity])
            for identity in identities
        ):
            raise ArtifactValidationError("Challenge 任务包含未通过 准入评估 的样本")
        for identity in identities:
            previous = challenge_by_identity.get(identity)
            if previous is not None and previous != validated:
                raise ArtifactValidationError("同一源 identity 对应多个 Challenge 结果")
            challenge_by_identity[identity] = validated
    files: JsonObject = {}
    candidate_rows_by_set: dict[str, list[JsonObject]] = {}
    final_l2_counts_by_set: dict[str, Counter[str]] = {}

    for set_name in ("P0", "P1", "P2"):
        seen_texts: set[str] = set()
        branch_root = run_root / set_name
        canonical_path = branch_root / "screened.canonical.jsonl"
        labeled_path = external_root(run_root) / LABELED_FILENAMES[set_name]
        provenance_path = branch_root / "export_provenance.jsonl"
        candidate_path = branch_root / "candidate_tasks.jsonl"
        branch_report_path = branch_root / "branch_report.json"
        canonical = load_jsonl(canonical_path)
        dedup_audit_path = branch_root / "dedup_audit.jsonl"
        dedup_audit = []
        dedup_rules = summary["config"]["sampling"]["dedup"]
        dedup_counts = Counter()
        for line_no, original in iter_jsonl(dedup_audit_path):
            row = _validate_dedup_audit_row(set_name, dedup_audit_path, line_no, original, dedup_rules)
            dedup_audit.append(original)
            dedup_counts[row["reason"]] += 1
        expected_dedup = summary["dedup"][set_name]
        if (dedup_counts["exact_normalized"] != expected_dedup["exact_dropped"]
                or dedup_counts["near_duplicate"] != expected_dedup["near_dropped"]
                or dedup_counts["below_confirmation_threshold"] != expected_dedup["borderline_retained"]):
            raise ArtifactValidationError(f"{set_name} dedup audit totals mismatch")
        labeled = load_jsonl(labeled_path)
        base_path = external_root(run_root) / base_filename(set_name)
        bases = load_jsonl(base_path)
        if bases != [{k: row[k] for k in base_fields(set_name)} for row in labeled] or any(tuple(row) != base_fields(set_name) for row in bases):
            raise ArtifactValidationError("基础版与 labeled 未逐行保持一致")
        provenance = load_jsonl(provenance_path)
        candidate_rows = load_jsonl(candidate_path)
        candidate_rows_by_set[set_name] = candidate_rows
        candidate_tasks = {str(row.get("task_id")): row for row in candidate_rows}
        if len(candidate_tasks) != len(candidate_rows) or "None" in candidate_tasks:
            raise ArtifactValidationError(f"{set_name} candidate task ID 非法或重复")
        set_summary = summary.get("sets", {}).get(set_name)
        if not isinstance(set_summary, dict) or type(set_summary.get("final")) is not int:
            raise ArtifactValidationError(f"{set_name} 构建汇总缺失")
        validate_screened_canonical_records(
            set_name,
            canonical,
            expected_candidate_tasks=candidate_tasks,
            expected_final_count=set_summary["final"],
        )
        if not (len(canonical) == len(labeled) == len(provenance)):
            raise ArtifactValidationError(f"{set_name} canonical/labeled/provenance 数量不一致")

        l2_counts: Counter[str] = Counter()
        for index, (canonical_row, labeled_row, provenance_row) in enumerate(
            zip(canonical, labeled, provenance), start=1
        ):
            quality = canonical_row["quality"]
            normalized = normalize_text(selection_text(canonical_row["candidate"]))
            if normalized in seen_texts:
                raise ArtifactValidationError(f"{set_name} has duplicate normalized text at row {index}")
            seen_texts.add(normalized)
            expected_labels = delivery_labels(canonical_row["candidate"], {"admission": quality["admission"]}, risk_snapshot)
            if canonical_row["delivery_labels"] != expected_labels:
                raise ArtifactValidationError(f"{set_name} 第 {index} 条下游标签不一致")
            validate_labeled_record(set_name, labeled_row, canonical_record=canonical_row)
            candidate = canonical_row["candidate"]
            selection = canonical_row["selection"]
            identity = candidate["source_identity_sha256"]
            expected_provenance = {
                "output_index": index,
                "base_record_sha256": stable_record_sha256(bases[index-1]),
                "source_identity_sha256": identity,
                "source_record_sha256": candidate["source"]["record_sha256"],
                "canonical_record_sha256": stable_record_sha256(canonical_row),
                "labeled_record_sha256": stable_record_sha256(labeled_row),
                "origin": selection["origin"],
                "round_id": selection["round_id"],
                "candidate_task_id": selection["candidate_task_id"],
                "admission_task_id": selection["admission_task_id"],
                "labeled_mapping_version": LABELED_MAPPING_VERSION,
            }
            if provenance_row != expected_provenance:
                raise ArtifactValidationError(f"{set_name} 第 {index} 条 provenance 不一致")
            for stage, tasks, results in (
                ("admission", admission_tasks, admission_results),
            ):
                task_id = selection[f"{stage}_task_id"]
                task = tasks.get(task_id)
                if task is None or task_id not in results:
                    raise ArtifactValidationError(f"{set_name} 第 {index} 条缺少 {stage} 合法终态")
                if identity not in task.get("source_identities", []):
                    raise ArtifactValidationError(f"{set_name} 第 {index} 条无法关联 {stage} 任务")
            expected_admission = {key: canonical_row["quality"][key] for key in ADMISSION_FIELDS}
            admission_value = validate_admission_result(admission_results[selection["admission_task_id"]])
            _validate_admission_binding(canonical_row, admission_tasks, admission_results, stage1_contract)
            if admission_value != expected_admission:
                raise ArtifactValidationError(f"{set_name} 第 {index} 条质量标签与结果账本不一致")
            if set_name == "P2":
                _validate_challenge_binding(canonical_row, challenge_tasks, challenge_results, stage1_contract)
                challenge = canonical_row["challenge"]
                challenge_task_id = challenge["challenge_task_id"]
                task = challenge_tasks.get(challenge_task_id)
                result = challenge_results.get(challenge_task_id)
                if task is None or result is None or identity not in task.get("source_identities", []):
                    raise ArtifactValidationError(f"P2 第 {index} 条无法关联 Challenge 合法终态")
                expected_challenge = {key: challenge[key] for key in CHALLENGE_FIELDS}
                if validate_challenge_result(result) != expected_challenge:
                    raise ArtifactValidationError(f"P2 第 {index} 条 Challenge 标签与结果账本不一致")
            l2_counts[candidate["taxonomy"]["l2_key"]] += 1
        final_l2_counts_by_set[set_name] = l2_counts

        if set_name == "P0" and any(
            row["selection"]["origin"] != "initial_wave" for row in canonical
        ):
            raise ArtifactValidationError(f"{set_name} 不得存在补采记录")
        if set_name == "P1":
            supplement = summary.get("p1_supplement")
            if not isinstance(supplement, dict):
                raise ArtifactValidationError("P1 补采汇总缺失")
            quotas = supplement.get("target_quotas_by_l2")
            deficits = supplement.get("deficits_by_l2")
            if not isinstance(quotas, dict) or not isinstance(deficits, dict):
                raise ArtifactValidationError("P1 冻结配额或缺口缺失")
            expected_deficits = {
                key: max(0, int(quota) - l2_counts.get(key, 0))
                for key, quota in quotas.items()
            }
            if deficits != expected_deficits:
                raise ArtifactValidationError("P1 逐 L2 缺口与最终记录不守恒")
            target = supplement.get("target")
            if (
                type(target) is not int
                or sum(int(value) for value in quotas.values())
                + int(supplement.get("unallocated_target", 0)) != target
                or int(supplement.get("total_deficit", -1)) != target - len(canonical)
            ):
                raise ArtifactValidationError("P1 目标、冻结配额与总缺口不守恒")
        if set_name == "P2":
            adaptive = summary.get("p2_adaptive")
            if not isinstance(adaptive, dict):
                raise ArtifactValidationError("P2 自适应汇总缺失")
            target = adaptive.get("target")
            candidate_source: dict[str, str] = {}
            for row in candidate_rows:
                identity = str(row.get("source_identity_sha256", ""))
                sampled_from = row.get("sampled_from")
                source_tags = row.get("source_tags")
                if (
                    sampled_from not in P2_SOURCE_NAMES
                    or not isinstance(source_tags, list)
                    or not source_tags
                    or any(tag not in P2_SOURCE_NAMES for tag in source_tags)
                    or sampled_from not in source_tags
                ):
                    raise ArtifactValidationError("P2 candidate 来源归因非法")
                candidate_source[identity] = str(sampled_from)
            admission_pass_ids = {
                identity for identity in candidate_source
                if admission_passes(admission_by_identity[identity])
            }
            if set(challenge_by_identity) != admission_pass_ids:
                raise ArtifactValidationError("P2 准入评估 通过样本与 Challenge 账本不一致")
            judge_pass_ids = {
                identity for identity in admission_pass_ids
                if challenge_by_identity[identity]["is_challenge"] is True
            }
            final_ids = {
                str(row["candidate"]["source_identity_sha256"])
                for row in canonical
            }
            primary = Counter(
                str(row["challenge"]["primary_challenge"]) for row in canonical
            )
            source_funnel = adaptive.get("source_funnel")
            if not isinstance(source_funnel, Mapping) or set(source_funnel) != set(P2_SOURCE_NAMES):
                raise ArtifactValidationError("P2 来源漏斗缺失")
            for source in P2_SOURCE_NAMES:
                if not isinstance(source_funnel[source], Mapping):
                    raise ArtifactValidationError(f"P2 来源池 {source} 漏斗结构非法")
                source_ids = {
                    identity for identity, value in candidate_source.items() if value == source
                }
                expected = {
                    "after_dedup": len(source_ids),
                    "admission_pass": len(source_ids & admission_pass_ids),
                    "judge_pass": len(source_ids & judge_pass_ids),
                    "final": len(source_ids & final_ids),
                }
                if any(source_funnel[source].get(key) != value for key, value in expected.items()):
                    raise ArtifactValidationError(f"P2 来源池 {source} 漏斗与任务账本不一致")
            if (
                type(target) is not int
                or adaptive.get("algorithm") != P2_ALGORITHM
                or adaptive.get("soft_min_by_primary")
                != dict(summary["config"]["sampling"]["p2"]["soft_min_by_primary"])
                or adaptive.get("max_per_primary")
                != int(summary["config"]["sampling"]["p2"]["max_per_primary"])
                or adaptive.get("final") != len(canonical)
                or adaptive.get("target_gap") != max(0, target - len(canonical))
                or adaptive.get("final_by_primary") != {
                    key: primary[key] for key in CHALLENGE_TYPES
                }
                or any(count > int(summary["config"]["sampling"]["p2"]["max_per_primary"])
                       for count in primary.values())
            ):
                raise ArtifactValidationError("P2 目标、类型上限或总缺口不守恒")

        report = _load_object(branch_report_path)["export"]
        if (
            report.get("set") != set_name
            or report.get("records") != len(canonical)
                or report.get("field_count") != len(labeled_fields(set_name))
                or report.get("fields") != list(labeled_fields(set_name))
                or report.get("rounded_numeric_fields") != ["norm_freq", "relevance_p0", *(("relevance_p2",) if set_name == "P2" else ())]
            or report.get("null_values") != 0
            or report.get("lossy_enum_mappings") != 0
        ):
            raise ArtifactValidationError(f"{set_name} labeled export report 不一致")
        if aggregate_export.get("sets", {}).get(set_name) != report:
            raise ArtifactValidationError(f"{set_name} 分支导出报告与总报告不一致")
        files[set_name] = {
            "quality_annotations": _quality_annotations(run_root, set_name, canonical, manifest, quality_contract),
            "canonical": _jsonl_metadata(canonical_path, run_root, canonical),
            "labeled": _jsonl_metadata(labeled_path, run_root, labeled),
            "base": _jsonl_metadata(base_path, run_root, bases),
            "provenance": _jsonl_metadata(provenance_path, run_root, provenance),
            "candidate_tasks": _jsonl_metadata(candidate_path, run_root, candidate_rows),
            "dedup_audit": _jsonl_metadata(branch_root / "dedup_audit.jsonl", run_root, dedup_audit),
            "export_report": {
                "path": branch_report_path.relative_to(run_root).as_posix(),
                "sha256": sha256_file(branch_report_path),
            },
            "branch_audit": {
                "path": f"{set_name}/branch_report.json",
                "sha256": sha256_file(branch_root / "branch_report.json"),
            },
        }
        if set_name == "P1":
            files[set_name]["supplement_summary"] = {
                "path": "P1/supplement_summary.json",
                "sha256": sha256_file(branch_root / "supplement_summary.json"),
            }
        if set_name == "P2":
            files[set_name]["adaptive_summary"] = {
                "path": "P2/adaptive_summary.json",
                "sha256": sha256_file(branch_root / "adaptive_summary.json"),
            }

    if _load_object(run_root / "reports/export_validation.json") != aggregate_export:
        raise ArtifactValidationError("导出校验报告与 manifest 导出结果不一致")
    root_files = {}
    if origin.exists():
        root_files["stage1_quality_contract"] = {"path": "snapshots/stage1_quality_contract.json", "sha256": sha256_file(origin)}
    for filename in ("snapshots/dedup_idf.json", "snapshots/risk_keywords.json", "snapshots/config.json", "snapshots/quality_contract.json", "reports/export_validation.json", "reports/字段标准与使用说明.md"):
        root_files[filename] = {"path": filename, "sha256": sha256_file(run_root / filename)}
    try:
        run_report = load_and_validate_run_report(run_root)
    except RunReportError as exc:
        raise ArtifactValidationError(f"批跑报告校验失败: {exc}") from exc
    if run_report.get("run_id") != manifest["run_id"]:
        raise ArtifactValidationError("批跑报告 run_id 与运行目录不一致")
    if run_report.get("dedup") != summary.get("dedup"):
        raise ArtifactValidationError("Run report dedup totals mismatch")
    if run_report.get("totals", {}).get("cross_set_overlap") != summary.get(
        "cross_set_overlap", {}
    ):
        raise ArtifactValidationError("批跑报告跨集合重叠统计不一致")
    for set_name in ("P0", "P1", "P2"):
        set_report = run_report["sets"][set_name]
        candidate_rows = candidate_rows_by_set[set_name]
        if set_report.get("admission_evaluated") != len(candidate_rows):
            raise ArtifactValidationError(f"{set_name} 批跑报告候选数量不一致")
        if set_report.get("final_count") != files[set_name]["canonical"]["records"]:
            raise ArtifactValidationError(f"{set_name} 批跑报告最终数量不一致")

        category = Counter()
        usable = 0
        exact_usable = 0
        passed = 0
        candidate_l2 = Counter()
        for task in candidate_rows:
            identity = str(task.get("source_identity_sha256", ""))
            result = admission_by_identity.get(identity)
            if result is None:
                raise ArtifactValidationError(f"{set_name} 候选无法关联准入评估结果")
            category[category_fit(result)] += 1
            is_usable = admission_usable(result)
            if is_usable:
                usable += 1
                if category_fit(result) == "exact":
                    exact_usable += 1
            if admission_passes(result):
                passed += 1
            candidate_l2[str(task["l2_key"])] += 1
        expected_issues = admission_issue_summary([
            admission_by_identity[str(task["source_identity_sha256"])] for task in candidate_rows
        ])
        if any(set_report.get(key) != value for key, value in expected_issues.items()):
            raise ArtifactValidationError(f"{set_name} 准入评估 阻断统计与账本不一致")
        final_rows = load_jsonl(run_root / set_name / "screened.canonical.jsonl")
        from ..quality.annotation import quality_report
        expected_quality = quality_report([
            {"source_identity_sha256": row["candidate"]["source_identity_sha256"],
             "task_id": row["provenance"]["quality_annotation_task_id"], "result": row["downstream_quality"]}
            for row in final_rows
        ])
        if set_report.get("downstream_quality") != expected_quality:
            raise ArtifactValidationError("Stage2 质量统计或复核分歧与逐条结果不一致")
        zero_reasons = Counter(reason for row in final_rows for reason in row["delivery_labels"]["executable_reasons"])
        from .eligibility import REASON_ORDER as executable_reason_order
        if set_report.get("executable_zero_reasons") != {reason: zero_reasons[reason] for reason in executable_reason_order}:
            raise ArtifactValidationError("executable 原因统计与内部记录不一致")
        if set_report.get("category_fit_distribution") != {
            key: category.get(key, 0) for key in ("exact", "partial", "mismatch", "not_provided")
        }:
            raise ArtifactValidationError(f"{set_name} 批跑报告类别分布不一致")
        if (
            set_report.get("usable_queries") != usable
            or set_report.get("category_exact_among_usable") != exact_usable
            or set_report.get("admission_pass") != passed
        ):
            raise ArtifactValidationError(f"{set_name} 批跑报告质量指标不一致")
        report_l2 = {
            str(row["l2_key"]): row
            for l1_row in set_report["semantic_tree"]
            for row in l1_row["l2"]
        }
        if any(
            int(report_l2[key]["admission_evaluated"]) != count
            for key, count in candidate_l2.items()
        ) or any(
            int(report_l2[key]["final_count"]) != count
            for key, count in final_l2_counts_by_set[set_name].items()
        ):
            raise ArtifactValidationError(f"{set_name} 批跑报告 L2 分布不一致")
    root_files["screening_run_report_json"] = {
        "path": RUN_REPORT_JSON,
        "sha256": sha256_file(run_root / RUN_REPORT_JSON),
    }
    root_files["screening_run_report_markdown"] = {
        "path": RUN_REPORT_MARKDOWN,
        "sha256": sha256_file(run_root / RUN_REPORT_MARKDOWN),
    }
    if (
        aggregate_export.get("total_records")
        != sum(files[name]["labeled"]["records"] for name in ("P0", "P1", "P2"))
        or aggregate_export.get("lossy_enum_mappings") != 0
    ):
        raise ArtifactValidationError("labeled 总导出报告数量或枚举映射不一致")
    quality_files: JsonObject = {}
    for filename in (
        "admission_tasks.jsonl", "admission_attempts.jsonl",
        "admission_cache_hits.jsonl", "admission_results.jsonl", "challenge_tasks.jsonl",
        "challenge_attempts.jsonl", "challenge_cache_hits.jsonl", "challenge_results.jsonl",
        *[f"{stage}_{suffix}.jsonl" for stage in ("admission_features", "admission_category", *quality_contract.get("additional_stages", [])) for suffix in ("tasks", "results", "attempts", "cache_hits")],
    ):
        path = run_root / "quality" / filename
        metadata: JsonObject = {
            "path": os.path.relpath(path, run_root).replace("\\", "/"),
            "sha256": sha256_file(path),
        }
        if filename.endswith(".jsonl"):
            metadata["records"] = len(load_jsonl(path))
        quality_files[filename] = metadata
    from .package import validate_package
    validate_package(run_root, manifest)
    external = {p.relative_to(external_root(run_root)).as_posix(): sha256_file(p)
                for p in external_root(run_root).rglob("*") if p.is_file()}
    return {"sets": files, "root": root_files, "quality": quality_files, "external": external}


def collect_and_validate_artifacts(run_root: Path) -> JsonObject:
    try:
        return _collect_and_validate_artifacts(run_root)
    except ArtifactValidationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ArtifactValidationError(f"交付或内部记录校验失败: {type(exc).__name__}: {exc}") from exc


def validate_manifest_artifacts(run_root: Path, manifest: Mapping[str, Any]) -> JsonObject:
    current = collect_and_validate_artifacts(run_root)
    if manifest.get("artifacts") != current:
        raise ArtifactValidationError("manifest 产物哈希、数量或路径与当前文件不一致")
    return current


def validate_manifest_source(manifest: Mapping[str, Any], *, verify_full_hash: bool = False) -> None:
    index = manifest.get("index")
    if not isinstance(index, Mapping):
        raise ArtifactValidationError("manifest 缺少 canonical index")
    metadata_path = Path(str(index.get("metadata_path", ""))).resolve()
    database_path = Path(str(index.get("database_path", ""))).resolve()
    metadata = _load_object(metadata_path)
    if sha256_file(metadata_path) != index.get("metadata_sha256"):
        raise ArtifactValidationError("canonical index metadata 已变化")
    if metadata.get("source_snapshot_id") != manifest.get("source", {}).get("sha256"):
        raise ArtifactValidationError("canonical index 与 manifest 源快照不一致")
    validate_canonical_source(metadata, verify_full_hash=verify_full_hash)
    handle = CanonicalIndexHandle(metadata_path.parent, database_path, metadata_path, metadata)
    validate_canonical_database(handle)


def validate_manifest_inputs(run_root: Path, manifest: Mapping[str, Any]) -> None:
    """Recheck frozen taxonomy and Prompt snapshots before reuse/delivery."""
    taxonomy = manifest.get("taxonomy")
    contract = manifest.get("contract")
    if not isinstance(taxonomy, Mapping) or not isinstance(contract, Mapping):
        raise ArtifactValidationError("manifest 缺少 taxonomy 或冻结契约")
    taxonomy_path = Path(str(taxonomy.get("path", ""))).resolve()
    if not taxonomy_path.is_file() or sha256_file(taxonomy_path) != taxonomy.get("sha256"):
        raise ArtifactValidationError("taxonomy 缺失或已变化")
    prompts = contract.get("prompts")
    if not isinstance(prompts, Mapping):
        raise ArtifactValidationError("manifest 缺少 Prompt 哈希")
    if set(prompts) != {"admission_sha256", "category_sha256", "challenge_sha256", "quality_annotation_sha256"}:
        raise ArtifactValidationError("manifest Prompt 集合非法")
    for key in prompts:
        stage = key.removesuffix("_sha256")
        path = prompt_path(run_root, stage)
        if not path.is_file() or sha256_file(path) != prompts.get(f"{stage}_sha256"):
            raise ArtifactValidationError(f"冻结的 {stage} Prompt 缺失或已变化")
    for component, original in (("admission_features", "admission"), ("admission_category", "category")):
        snapshot = prompt_path(run_root, component)
        if not snapshot.is_file() or sha256_file(snapshot) != prompts[f"{original}_sha256"]:
            raise ArtifactValidationError("准入评估 组件 Prompt 与 manifest 不一致")
