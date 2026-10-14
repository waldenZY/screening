"""Strict artifact validation and transactional publication."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
from .legacy import (
    LEGACY_FILENAMES,
    LEGACY_MAPPING_VERSION,
    legacy_fields,
    map_screened_to_legacy_unchecked,
    validate_legacy_record,
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
from ..quality.two_stage import (
    CHALLENGE_FIELDS,
    CHALLENGE_TYPES,
    QUALITY_CONTRACT_VERSION,
    STAGE1_FIELDS,
    STAGE2_FIELDS,
    stage1_passes,
    stage1_quality_input,
    stage1_input_sha256,
    stage1_task_key,
    stage1_issue_summary,
    stage2_semantic_review,
    stage2_input_sha256,
    stage2_task_key,
    validate_challenge_result,
    validate_stage1_result,
    validate_stage2_result,
)


class PublicationError(ValueError):
    """A v3 artifact or safe-publication contract violation."""


def _log_content_conflict(set_name: str, destination: Path, *, replace: bool) -> None:
    logging.getLogger(__name__).warning("publication %s", json.dumps({
        "check": "content_conflict", "set": set_name, "path": str(destination),
        "next_action": "transactional_replace" if replace else "stop_without_replace",
    }, ensure_ascii=False, sort_keys=True))


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
        raise PublicationError(f"{set_name} {message}: " + json.dumps(details, ensure_ascii=False, sort_keys=True))

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
            or contract.get("legacy_mapping_version") != LEGACY_MAPPING_VERSION
            or contract.get("dedup_rules_sha256") != RULES_SHA256
            or not isinstance(contract.get("dedup_idf_sha256"), str)):
        raise PublicationError("Old dedup or mapping contract cannot be published")
    if not isinstance(contract, Mapping) or contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION:
        raise PublicationError("质量契约版本不一致，旧 run 必须重新运行")


def _load_object(path: Path) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"无法读取运行元数据: {path.name}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"运行元数据必须是对象: {path.name}")
    return value


def _jsonl_metadata(path: Path, run_root: Path, records: list[JsonObject]) -> JsonObject:
    return {
        "path": path.relative_to(run_root).as_posix(),
        "sha256": sha256_file(path),
        "records": len(records),
    }


def _task_maps(
    run_root: Path,
) -> tuple[dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject]]:
    quality_root = run_root / "quality"
    stage1_task_rows = load_jsonl(quality_root / "stage1_tasks.jsonl")
    stage2_task_rows = load_jsonl(quality_root / "stage2_tasks.jsonl")
    challenge_task_rows = load_jsonl(quality_root / "challenge_tasks.jsonl")
    stage1_result_rows = load_jsonl(quality_root / "stage1_results.jsonl")
    stage2_result_rows = load_jsonl(quality_root / "stage2_results.jsonl")
    challenge_result_rows = load_jsonl(quality_root / "challenge_results.jsonl")
    stage1_tasks = {row.get("cache_key"): row for row in stage1_task_rows}
    stage2_tasks = {row.get("cache_key"): row for row in stage2_task_rows}
    challenge_tasks = {row.get("cache_key"): row for row in challenge_task_rows}
    stage1_results = {row.get("cache_key"): row.get("result") for row in stage1_result_rows}
    stage2_results = {row.get("cache_key"): row.get("result") for row in stage2_result_rows}
    challenge_results = {row.get("cache_key"): row.get("result") for row in challenge_result_rows}
    if None in stage1_tasks or len(stage1_tasks) != len(stage1_task_rows):
        raise PublicationError("第一阶段任务账本存在重复 ID")
    if None in stage2_tasks or len(stage2_tasks) != len(stage2_task_rows):
        raise PublicationError("第二阶段任务账本存在重复 ID")
    if None in challenge_tasks or len(challenge_tasks) != len(challenge_task_rows):
        raise PublicationError("Challenge 任务账本存在重复 ID")
    if None in stage1_results or len(stage1_results) != len(stage1_result_rows):
        raise PublicationError("第一阶段结果账本存在非法或重复 ID")
    if None in stage2_results or len(stage2_results) != len(stage2_result_rows):
        raise PublicationError("第二阶段结果账本存在非法或重复 ID")
    if None in challenge_results or len(challenge_results) != len(challenge_result_rows):
        raise PublicationError("Challenge 结果账本存在非法或重复 ID")
    if any(row.get("stage") != "stage1" for row in [*stage1_task_rows, *stage1_result_rows]):
        raise PublicationError("第一阶段账本 stage 标记非法")
    if any(row.get("stage") != "stage2" for row in [*stage2_task_rows, *stage2_result_rows]):
        raise PublicationError("第二阶段账本 stage 标记非法")
    if any(row.get("stage") != "challenge" for row in [*challenge_task_rows, *challenge_result_rows]):
        raise PublicationError("Challenge 账本 stage 标记非法")
    for stage, task_map in (
        ("stage1", stage1_tasks),
        ("challenge", challenge_tasks),
        ("stage2", stage2_tasks),
    ):
        cache_hit_rows = load_jsonl(quality_root / f"{stage}_cache_hits.jsonl")
        cache_hit_ids = {row.get("event_id") for row in cache_hit_rows}
        if None in cache_hit_ids or len(cache_hit_ids) != len(cache_hit_rows):
            raise PublicationError(f"{stage} 缓存命中账本存在非法或重复 ID")
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
                raise PublicationError(f"{stage} 缓存命中账本内容非法")
    for result in stage1_results.values():
        validate_stage1_result(result)
    for result in challenge_results.values():
        validate_challenge_result(result)
    return stage1_tasks, challenge_tasks, stage2_tasks, stage1_results, challenge_results, stage2_results


def _validate_stage2_binding(
    record: Mapping[str, Any],
    tasks: Mapping[str, JsonObject],
    results: Mapping[str, JsonObject],
    quality_contract: Mapping[str, Any],
) -> None:
    """Reconcile one member with the exact independent Stage2 input and result."""
    candidate = record["candidate"]
    task_id = record["selection"]["stage2_task_id"]
    task = tasks.get(task_id)
    prompt_hashes = quality_contract.get("prompt_hashes")
    options_hash = quality_contract.get("options_sha256")
    if (
        quality_contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION
        or not isinstance(prompt_hashes, Mapping)
        or not isinstance(prompt_hashes.get("stage2"), str)
        or not isinstance(options_hash, str)
    ):
        raise PublicationError("Stage2 质量运行契约非法")
    prompt_hash = prompt_hashes["stage2"]
    expected_id = stage2_task_key(
        candidate, prompt_sha256=prompt_hash, options_sha256=options_hash,
    )
    if (
        task_id != expected_id
        or not isinstance(task, Mapping)
        or task.get("stage") != "stage2"
        or task.get("input_sha256") != stage2_input_sha256(candidate)
        or task.get("prompt_sha256") != prompt_hash
        or task.get("options_sha256") != options_hash
        or "taxonomy_sha256" in task
        or not isinstance(task.get("source_identities"), list)
        or not isinstance(task.get("source_lines"), list)
        or candidate["source_identity_sha256"] not in task.get("source_identities", [])
        or candidate["source"]["source_line"] not in task.get("source_lines", [])
        or task_id not in results
    ):
        raise PublicationError("Stage2 输入哈希或任务关联不一致")
    stage2 = validate_stage2_result(results[task_id])
    if stage2 != {key: record["quality"][key] for key in STAGE2_FIELDS}:
        raise PublicationError("Stage2 标签与结果账本不一致")


def _validate_stage1_binding(
    record: Mapping[str, Any],
    tasks: Mapping[str, JsonObject],
    results: Mapping[str, JsonObject],
    quality_contract: Mapping[str, Any],
) -> None:
    """Verify admitted members against the actual Stage1 payload and ledger."""
    candidate = record["candidate"]
    task_id = record["selection"]["stage1_task_id"]
    task = tasks.get(task_id)
    prompt_hashes = quality_contract.get("prompt_hashes")
    options_hash = quality_contract.get("options_sha256")
    if (quality_contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION
            or not isinstance(prompt_hashes, Mapping)
            or not isinstance(prompt_hashes.get("stage1"), str)
            or not isinstance(options_hash, str)):
        raise PublicationError("Stage1 质量运行契约非法")
    prompt_hash = prompt_hashes["stage1"]
    if (task_id != stage1_task_key(candidate, prompt_sha256=prompt_hash, options_sha256=options_hash)
            or not isinstance(task, Mapping) or task.get("stage") != "stage1"
            or task.get("input_sha256") != stage1_input_sha256(candidate)
            or task.get("candidate_category_sha256") != stable_hash(stage1_quality_input(candidate)["candidate_category"])
            or task.get("prompt_sha256") != prompt_hash
            or task.get("options_sha256") != options_hash or "taxonomy_sha256" in task
            or not isinstance(task.get("source_identities"), list)
            or not isinstance(task.get("source_lines"), list)
            or candidate["source_identity_sha256"] not in task.get("source_identities", [])
            or candidate["source"]["source_line"] not in task.get("source_lines", [])
            or task_id not in results):
        raise PublicationError("Stage1 输入哈希或任务关联不一致")
    result = validate_stage1_result(results[task_id])
    if not stage1_passes(result) or result != {key: record["quality"][key] for key in STAGE1_FIELDS}:
        raise PublicationError("Stage1 准入标签与结果账本不一致")


def collect_and_validate_artifacts(run_root: Path) -> JsonObject:
    """Independently validate every authoritative and compatibility artifact."""
    run_root = run_root.expanduser().resolve()
    manifest = _load_object(run_root / "manifest.json")
    _require_current_quality_contract(manifest)
    idf_snapshot = _load_object(run_root / "dedup_idf.json")
    idf_content = {key: value for key, value in idf_snapshot.items() if key != "content_sha256"}
    if (idf_snapshot.get("rules_sha256") != RULES_SHA256
            or idf_snapshot.get("source_snapshot_id") != manifest.get("source", {}).get("sha256")
            or stable_hash(idf_content) != idf_snapshot.get("content_sha256")
            or idf_snapshot.get("content_sha256") != manifest["contract"].get("dedup_idf_sha256")):
        raise PublicationError("Frozen dedup IDF snapshot mismatch")
    risk_snapshot = _load_object(run_root / "risk_keywords.json")
    validate_risk_snapshot(risk_snapshot)
    if (risk_snapshot["source_sha256"] != manifest.get("contract", {}).get("risk_keywords_source_sha256")
            or risk_snapshot["rules_sha256"] != manifest.get("contract", {}).get("risk_keywords_rules_sha256")):
        raise PublicationError("风险词表快照与 run 契约不一致")
    summary = _load_object(run_root / "p012_build_summary.json")
    aggregate_export = _load_object(run_root / "legacy_export_report.json")
    if aggregate_export.get("legacy_mapping_version") != LEGACY_MAPPING_VERSION:
        raise PublicationError("legacy mapping version 不一致")
    stage1_tasks, challenge_tasks, stage2_tasks, stage1_results, challenge_results, stage2_results = _task_maps(run_root)
    quality_contract = _load_object(run_root / "quality" / "quality_run_contract.json")
    stage1_by_identity: dict[str, JsonObject] = {}
    for task_id, task in stage1_tasks.items():
        result = stage1_results.get(task_id)
        if result is None:
            raise PublicationError("第一阶段任务缺少合法终态")
        validated = validate_stage1_result(result)
        for identity in task.get("source_identities", []):
            previous = stage1_by_identity.get(str(identity))
            if previous is not None and previous != validated:
                raise PublicationError("同一源 identity 对应多个第一阶段结果")
            stage1_by_identity[str(identity)] = validated
    if set(challenge_results) != set(challenge_tasks):
        raise PublicationError("Challenge 任务与结果账本不一致")
    challenge_by_identity: dict[str, JsonObject] = {}
    for task_id, task in challenge_tasks.items():
        validated = validate_challenge_result(challenge_results[task_id])
        identities = [str(value) for value in task.get("source_identities", [])]
        if not identities or any(
            identity not in stage1_by_identity or not stage1_passes(stage1_by_identity[identity])
            for identity in identities
        ):
            raise PublicationError("Challenge 任务包含未通过 Stage1 的样本")
        for identity in identities:
            previous = challenge_by_identity.get(identity)
            if previous is not None and previous != validated:
                raise PublicationError("同一源 identity 对应多个 Challenge 结果")
            challenge_by_identity[identity] = validated
    if set(stage2_results) != set(stage2_tasks):
        raise PublicationError("第二阶段任务缺少合法终态")
    files: JsonObject = {}
    candidate_rows_by_set: dict[str, list[JsonObject]] = {}
    final_l2_counts_by_set: dict[str, Counter[str]] = {}

    for set_name in ("P0", "P1", "P2"):
        seen_texts: set[str] = set()
        branch_root = run_root / set_name
        canonical_path = branch_root / "screened.canonical.jsonl"
        legacy_path = branch_root / LEGACY_FILENAMES[set_name].name
        provenance_path = branch_root / "export_provenance.jsonl"
        candidate_path = branch_root / "candidate_tasks.jsonl"
        branch_report_path = branch_root / "legacy_export_report.json"
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
            raise PublicationError(f"{set_name} dedup audit totals mismatch")
        legacy = load_jsonl(legacy_path)
        provenance = load_jsonl(provenance_path)
        candidate_rows = load_jsonl(candidate_path)
        candidate_rows_by_set[set_name] = candidate_rows
        candidate_tasks = {str(row.get("task_id")): row for row in candidate_rows}
        if len(candidate_tasks) != len(candidate_rows) or "None" in candidate_tasks:
            raise PublicationError(f"{set_name} candidate task ID 非法或重复")
        set_summary = summary.get("sets", {}).get(set_name)
        if not isinstance(set_summary, dict) or type(set_summary.get("final")) is not int:
            raise PublicationError(f"{set_name} 构建汇总缺失")
        validate_screened_canonical_records(
            set_name,
            canonical,
            expected_candidate_tasks=candidate_tasks,
            expected_final_count=set_summary["final"],
        )
        if not (len(canonical) == len(legacy) == len(provenance)):
            raise PublicationError(f"{set_name} canonical/legacy/provenance 数量不一致")

        l2_counts: Counter[str] = Counter()
        for index, (canonical_row, legacy_row, provenance_row) in enumerate(
            zip(canonical, legacy, provenance), start=1
        ):
            quality = canonical_row["quality"]
            normalized = normalize_text(selection_text(canonical_row["candidate"]))
            if normalized in seen_texts:
                raise PublicationError(f"{set_name} has duplicate normalized text at row {index}")
            seen_texts.add(normalized)
            expected_labels = delivery_labels(canonical_row["candidate"], quality, risk_snapshot)
            if canonical_row["delivery_labels"] != expected_labels:
                raise PublicationError(f"{set_name} 第 {index} 条下游标签不一致")
            validate_legacy_record(set_name, legacy_row, canonical_record=canonical_row)
            if legacy_row != map_screened_to_legacy_unchecked(canonical_row):
                raise PublicationError(f"{set_name} 第 {index} 条 legacy 映射不一致")
            candidate = canonical_row["candidate"]
            selection = canonical_row["selection"]
            identity = candidate["source_identity_sha256"]
            expected_provenance = {
                "output_index": index,
                "source_identity_sha256": identity,
                "source_record_sha256": candidate["source"]["record_sha256"],
                "canonical_record_sha256": stable_record_sha256(canonical_row),
                "legacy_record_sha256": stable_record_sha256(legacy_row),
                "origin": selection["origin"],
                "round_id": selection["round_id"],
                "candidate_task_id": selection["candidate_task_id"],
                "stage1_task_id": selection["stage1_task_id"],
                "stage2_task_id": selection["stage2_task_id"],
                "legacy_mapping_version": LEGACY_MAPPING_VERSION,
            }
            if provenance_row != expected_provenance:
                raise PublicationError(f"{set_name} 第 {index} 条 provenance 不一致")
            for stage, tasks, results in (
                ("stage1", stage1_tasks, stage1_results),
                ("stage2", stage2_tasks, stage2_results),
            ):
                task_id = selection[f"{stage}_task_id"]
                task = tasks.get(task_id)
                if task is None or task_id not in results:
                    raise PublicationError(f"{set_name} 第 {index} 条缺少 {stage} 合法终态")
                if identity not in task.get("source_identities", []):
                    raise PublicationError(f"{set_name} 第 {index} 条无法关联 {stage} 任务")
            expected_stage1 = {key: canonical_row["quality"][key] for key in STAGE1_FIELDS}
            stage1_value = validate_stage1_result(stage1_results[selection["stage1_task_id"]])
            _validate_stage1_binding(canonical_row, stage1_tasks, stage1_results, quality_contract)
            _validate_stage2_binding(canonical_row, stage2_tasks, stage2_results, quality_contract)
            if stage1_value != expected_stage1:
                raise PublicationError(f"{set_name} 第 {index} 条质量标签与结果账本不一致")
            if set_name == "P2":
                challenge = canonical_row["challenge"]
                challenge_task_id = challenge["challenge_task_id"]
                task = challenge_tasks.get(challenge_task_id)
                result = challenge_results.get(challenge_task_id)
                if task is None or result is None or identity not in task.get("source_identities", []):
                    raise PublicationError(f"P2 第 {index} 条无法关联 Challenge 合法终态")
                expected_challenge = {key: challenge[key] for key in CHALLENGE_FIELDS}
                if validate_challenge_result(result) != expected_challenge:
                    raise PublicationError(f"P2 第 {index} 条 Challenge 标签与结果账本不一致")
            l2_counts[candidate["taxonomy"]["l2_key"]] += 1
        final_l2_counts_by_set[set_name] = l2_counts

        if set_name == "P0" and any(
            row["selection"]["origin"] != "initial_wave" for row in canonical
        ):
            raise PublicationError(f"{set_name} 不得存在补采记录")
        if set_name == "P1":
            supplement = summary.get("p1_supplement")
            if not isinstance(supplement, dict):
                raise PublicationError("P1 补采汇总缺失")
            quotas = supplement.get("target_quotas_by_l2")
            deficits = supplement.get("deficits_by_l2")
            if not isinstance(quotas, dict) or not isinstance(deficits, dict):
                raise PublicationError("P1 冻结配额或缺口缺失")
            expected_deficits = {
                key: max(0, int(quota) - l2_counts.get(key, 0))
                for key, quota in quotas.items()
            }
            if deficits != expected_deficits:
                raise PublicationError("P1 逐 L2 缺口与最终记录不守恒")
            target = supplement.get("target")
            if (
                type(target) is not int
                or sum(int(value) for value in quotas.values())
                + int(supplement.get("unallocated_target", 0)) != target
                or int(supplement.get("total_deficit", -1)) != target - len(canonical)
            ):
                raise PublicationError("P1 目标、冻结配额与总缺口不守恒")
        if set_name == "P2":
            adaptive = summary.get("p2_adaptive")
            if not isinstance(adaptive, dict):
                raise PublicationError("P2 自适应汇总缺失")
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
                    raise PublicationError("P2 candidate 来源归因非法")
                candidate_source[identity] = str(sampled_from)
            stage1_pass_ids = {
                identity for identity in candidate_source
                if stage1_passes(stage1_by_identity[identity])
            }
            if set(challenge_by_identity) != stage1_pass_ids:
                raise PublicationError("P2 Stage1 通过样本与 Challenge 账本不一致")
            judge_pass_ids = {
                identity for identity in stage1_pass_ids
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
                raise PublicationError("P2 来源漏斗缺失")
            for source in P2_SOURCE_NAMES:
                if not isinstance(source_funnel[source], Mapping):
                    raise PublicationError(f"P2 来源池 {source} 漏斗结构非法")
                source_ids = {
                    identity for identity, value in candidate_source.items() if value == source
                }
                expected = {
                    "after_dedup": len(source_ids),
                    "stage1_pass": len(source_ids & stage1_pass_ids),
                    "judge_pass": len(source_ids & judge_pass_ids),
                    "final": len(source_ids & final_ids),
                }
                if any(source_funnel[source].get(key) != value for key, value in expected.items()):
                    raise PublicationError(f"P2 来源池 {source} 漏斗与任务账本不一致")
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
                raise PublicationError("P2 目标、类型上限或总缺口不守恒")

        report = _load_object(branch_report_path)
        if (
            report.get("set") != set_name
            or report.get("records") != len(canonical)
                or report.get("field_count") != len(legacy_fields(set_name))
                or report.get("fields") != list(legacy_fields(set_name))
                or report.get("rounded_numeric_fields") != []
            or report.get("null_values") != 0
            or report.get("lossy_enum_mappings") != 0
        ):
            raise PublicationError(f"{set_name} legacy export report 不一致")
        if aggregate_export.get("sets", {}).get(set_name) != report:
            raise PublicationError(f"{set_name} 分支导出报告与总报告不一致")
        files[set_name] = {
            "canonical": _jsonl_metadata(canonical_path, run_root, canonical),
            "legacy": _jsonl_metadata(legacy_path, run_root, legacy),
            "provenance": _jsonl_metadata(provenance_path, run_root, provenance),
            "candidate_tasks": _jsonl_metadata(candidate_path, run_root, candidate_rows),
            "dedup_audit": _jsonl_metadata(branch_root / "dedup_audit.jsonl", run_root, dedup_audit),
            "export_report": {
                "path": branch_report_path.relative_to(run_root).as_posix(),
                "sha256": sha256_file(branch_report_path),
            },
            "branch_audit": {
                "path": f"{set_name}/branch_audit.json",
                "sha256": sha256_file(branch_root / "branch_audit.json"),
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

    root_files = {
        "dedup_idf": {"path": "dedup_idf.json", "sha256": sha256_file(run_root / "dedup_idf.json")},
        "p012_build_summary": {
            "path": "p012_build_summary.json",
            "sha256": sha256_file(run_root / "p012_build_summary.json"),
        },
        "legacy_export_report": {
            "path": "legacy_export_report.json",
            "sha256": sha256_file(run_root / "legacy_export_report.json"),
        },
        "p012_run_contract": {
            "path": "p012_run_contract.json",
            "sha256": sha256_file(run_root / "p012_run_contract.json"),
        },
    }
    try:
        run_report = load_and_validate_run_report(run_root)
    except RunReportError as exc:
        raise PublicationError(f"批跑报告校验失败: {exc}") from exc
    if run_report.get("run_id") != run_root.name:
        raise PublicationError("批跑报告 run_id 与运行目录不一致")
    if run_report.get("dedup") != summary.get("dedup"):
        raise PublicationError("Run report dedup totals mismatch")
    if run_report.get("totals", {}).get("cross_set_overlap") != summary.get(
        "cross_set_overlap", {}
    ):
        raise PublicationError("批跑报告跨集合重叠统计不一致")
    for set_name in ("P0", "P1", "P2"):
        set_report = run_report["sets"][set_name]
        candidate_rows = candidate_rows_by_set[set_name]
        if set_report.get("stage1_evaluated") != len(candidate_rows):
            raise PublicationError(f"{set_name} 批跑报告候选数量不一致")
        if set_report.get("final_count") != files[set_name]["canonical"]["records"]:
            raise PublicationError(f"{set_name} 批跑报告最终数量不一致")

        category = Counter()
        usable = 0
        exact_usable = 0
        passed = 0
        candidate_l2 = Counter()
        for task in candidate_rows:
            identity = str(task.get("source_identity_sha256", ""))
            result = stage1_by_identity.get(identity)
            if result is None:
                raise PublicationError(f"{set_name} 候选无法关联第一阶段结果")
            category[result["category_fit"]] += 1
            is_usable = (
                result["semantic_completeness"] == "self_contained"
                and result["intent_clarity"] == "clear"
                and result["validity"] == "valid"
                and not result["blocking_issues"]
            )
            if is_usable:
                usable += 1
                if result["category_fit"] == "exact":
                    exact_usable += 1
            if stage1_passes(result):
                passed += 1
            candidate_l2[str(task["l2_key"])] += 1
        expected_issues = stage1_issue_summary([
            stage1_by_identity[str(task["source_identity_sha256"])] for task in candidate_rows
        ])
        if any(set_report.get(key) != value for key, value in expected_issues.items()):
            raise PublicationError(f"{set_name} Stage1 阻断统计与账本不一致")
        final_rows = load_jsonl(run_root / set_name / "screened.canonical.jsonl")
        if set_report.get("stage2_semantic_review") != stage2_semantic_review([
            {key: row["quality"][key] for key in STAGE2_FIELDS} for row in final_rows
        ]):
            raise PublicationError(f"{set_name} Stage2 语义问题复核统计不一致")
        if set_report.get("category_fit_distribution") != {
            key: category.get(key, 0) for key in ("exact", "partial", "mismatch")
        }:
            raise PublicationError(f"{set_name} 批跑报告类别分布不一致")
        if (
            set_report.get("usable_queries") != usable
            or set_report.get("category_exact_among_usable") != exact_usable
            or set_report.get("stage1_pass") != passed
        ):
            raise PublicationError(f"{set_name} 批跑报告质量指标不一致")
        report_l2 = {
            str(row["l2_key"]): row
            for l1_row in set_report["semantic_tree"]
            for row in l1_row["l2"]
        }
        if any(
            int(report_l2[key]["stage1_evaluated"]) != count
            for key, count in candidate_l2.items()
        ) or any(
            int(report_l2[key]["final_count"]) != count
            for key, count in final_l2_counts_by_set[set_name].items()
        ):
            raise PublicationError(f"{set_name} 批跑报告 L2 分布不一致")
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
        != sum(files[name]["legacy"]["records"] for name in ("P0", "P1", "P2"))
        or aggregate_export.get("lossy_enum_mappings") != 0
    ):
        raise PublicationError("legacy 总导出报告数量或枚举映射不一致")
    quality_files: JsonObject = {}
    for filename in (
        "quality_run_contract.json", "stage1_tasks.jsonl", "stage1_attempts.jsonl",
        "stage1_cache_hits.jsonl", "stage1_results.jsonl", "challenge_tasks.jsonl",
        "challenge_attempts.jsonl", "challenge_cache_hits.jsonl", "challenge_results.jsonl",
        "stage2_tasks.jsonl", "stage2_attempts.jsonl", "stage2_cache_hits.jsonl",
        "stage2_results.jsonl",
    ):
        path = run_root / "quality" / filename
        metadata: JsonObject = {
            "path": path.relative_to(run_root).as_posix(),
            "sha256": sha256_file(path),
        }
        if filename.endswith(".jsonl"):
            metadata["records"] = len(load_jsonl(path))
        quality_files[filename] = metadata
    return {"sets": files, "root": root_files, "quality": quality_files}


def validate_manifest_artifacts(run_root: Path, manifest: Mapping[str, Any]) -> JsonObject:
    current = collect_and_validate_artifacts(run_root)
    if manifest.get("artifacts") != current:
        raise PublicationError("manifest 产物哈希、数量或路径与当前文件不一致")
    return current


def validate_manifest_source(manifest: Mapping[str, Any], *, verify_full_hash: bool = False) -> None:
    index = manifest.get("index")
    if not isinstance(index, Mapping):
        raise PublicationError("manifest 缺少 canonical index")
    metadata_path = Path(str(index.get("metadata_path", ""))).resolve()
    database_path = Path(str(index.get("database_path", ""))).resolve()
    metadata = _load_object(metadata_path)
    if sha256_file(metadata_path) != index.get("metadata_sha256"):
        raise PublicationError("canonical index metadata 已变化")
    if metadata.get("source_snapshot_id") != manifest.get("source", {}).get("sha256"):
        raise PublicationError("canonical index 与 manifest 源快照不一致")
    validate_canonical_source(metadata, verify_full_hash=verify_full_hash)
    handle = CanonicalIndexHandle(metadata_path.parent, database_path, metadata_path, metadata)
    validate_canonical_database(handle)


def validate_manifest_inputs(run_root: Path, manifest: Mapping[str, Any]) -> None:
    """Recheck frozen taxonomy and Prompt snapshots before reuse/publication."""
    taxonomy = manifest.get("taxonomy")
    contract = manifest.get("contract")
    if not isinstance(taxonomy, Mapping) or not isinstance(contract, Mapping):
        raise PublicationError("manifest 缺少 taxonomy 或冻结契约")
    taxonomy_path = Path(str(taxonomy.get("path", ""))).resolve()
    if not taxonomy_path.is_file() or sha256_file(taxonomy_path) != taxonomy.get("sha256"):
        raise PublicationError("taxonomy 缺失或已变化")
    prompts = contract.get("prompts")
    if not isinstance(prompts, Mapping):
        raise PublicationError("manifest 缺少 Prompt 哈希")
    for stage in ("stage1", "challenge", "stage2"):
        path = run_root / "prompts" / f"{stage}.md"
        if not path.is_file() or sha256_file(path) != prompts.get(f"{stage}_sha256"):
            raise PublicationError(f"冻结的 {stage} Prompt 缺失或已变化")


def _transactional_publish(files: list[tuple[Path, Path]]) -> None:
    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for source, destination in files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".publish.tmp",
            )
            temporary = Path(name)
            with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(source_handle, target)
            if sha256_file(source) != sha256_file(temporary):
                raise OSError("发布临时文件哈希校验失败")
            staged.append((temporary, destination))
        for temporary, destination in staged:
            backup: Path | None = None
            if destination.exists():
                descriptor, name = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".backup.tmp",
                )
                os.close(descriptor)
                backup = Path(name)
                backup.unlink()
                os.replace(destination, backup)
            backups[destination] = backup
            os.replace(temporary, destination)
            committed.append(destination)
    except Exception:
        for destination in reversed(committed):
            destination.unlink(missing_ok=True)
            backup = backups.get(destination)
            if backup is not None and backup.exists():
                os.replace(backup, destination)
        for destination, backup in backups.items():
            if destination not in committed and backup is not None and backup.exists():
                os.replace(backup, destination)
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def publish_labeled(
    run_root: Path,
    publish_root: Path,
    manifest: Mapping[str, Any],
    *,
    replace: bool = False,
) -> JsonObject:
    """Publish only the three labeled files after full v3 validation."""
    run_root = run_root.expanduser().resolve()
    publish_root = publish_root.expanduser().resolve()
    _require_current_quality_contract(manifest)
    if manifest.get("pipeline_version") != PIPELINE_VERSION:
        raise PublicationError("仅允许发布当前 screening run")
    required_stages = (
        "canonical_index", "p012_build", "quality_stage1", "p1_supplement",
        "p2_supplement", "quality_p2_challenge", "quality_stage2",
        "screened_export", "legacy_export", "batch_report",
    )
    incomplete = [
        name
        for name in required_stages
        if manifest.get("stages", {}).get(name, {}).get("status") != "completed"
    ]
    if incomplete:
        raise PublicationError(f"以下阶段未完成，禁止发布: {', '.join(incomplete)}")
    if publish_root == run_root or publish_root.is_relative_to(run_root):
        raise PublicationError("publish_root 不能等于或位于 run 目录内")
    validate_manifest_inputs(run_root, manifest)
    validate_manifest_source(manifest)
    artifacts = validate_manifest_artifacts(run_root, manifest)
    pending: list[tuple[Path, Path]] = []
    files: JsonObject = {}
    conflicts: list[str] = []
    for set_name in ("P0", "P1", "P2"):
        source = run_root / artifacts["sets"][set_name]["legacy"]["path"]
        destination = publish_root / LEGACY_FILENAMES[set_name]
        source_hash = sha256_file(source)
        if destination.is_file():
            if sha256_file(destination) != source_hash:
                _log_content_conflict(set_name, destination, replace=replace)
                conflicts.append(str(destination))
                if replace:
                    pending.append((source, destination))
        else:
            pending.append((source, destination))
        files[set_name] = {
            "destination": str(destination),
            "sha256": source_hash,
            "records": artifacts["sets"][set_name]["legacy"]["records"],
        }
    if conflicts and not replace:
        raise PublicationError(
            "发布目标已有不同内容；如确认整组替换请使用 --replace"
        )
    _transactional_publish(pending)
    if any(not Path(value["destination"]).is_file() or sha256_file(Path(value["destination"])) != value["sha256"] for value in files.values()):
        raise PublicationError("发布后文件校验失败")
    return {"publish_root": str(publish_root), "replaced": bool(conflicts), "files": files}


def publish_labeled_branch(
    run_root: Path,
    publish_root: Path,
    manifest: Mapping[str, Any],
    set_name: str,
    *,
    replace: bool = False,
) -> JsonObject:
    """Validate and transactionally publish one completed branch."""
    if set_name not in LEGACY_FILENAMES:
        raise PublicationError(f"未知发布分支: {set_name}")
    _require_current_quality_contract(manifest)
    if manifest.get("pipeline_version") != PIPELINE_VERSION:
        raise PublicationError("仅允许发布当前 screening run")
    branch_state = manifest.get("branches", {}).get(set_name, {})
    if branch_state.get("status") not in {"completed", "published"}:
        raise PublicationError(f"{set_name} 尚未完成，禁止发布")
    run_root = run_root.expanduser().resolve()
    publish_root = publish_root.expanduser().resolve()
    if publish_root == run_root or publish_root.is_relative_to(run_root):
        raise PublicationError("publish_root 不能等于或位于 run 目录内")
    validate_manifest_inputs(run_root, manifest)
    validate_manifest_source(manifest)
    branch_root = run_root / set_name
    canonical_path = branch_root / "screened.canonical.jsonl"
    legacy_path = branch_root / LEGACY_FILENAMES[set_name].name
    provenance_path = branch_root / "export_provenance.jsonl"
    report_path = branch_root / "legacy_export_report.json"
    branch_report_path = branch_root / "branch_report.json"
    artifacts = branch_state.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise PublicationError(f"{set_name} 缺少分支产物快照")
    for relative_path, expected_hash in artifacts.items():
        artifact_path = branch_root / str(relative_path)
        if not artifact_path.is_file() or sha256_file(artifact_path) != expected_hash:
            raise PublicationError(f"{set_name} 分支产物缺失或哈希不一致: {relative_path}")
    canonical = load_jsonl(canonical_path)
    legacy = load_jsonl(legacy_path)
    provenance = load_jsonl(provenance_path)
    validate_screened_canonical_records(set_name, canonical)
    if len(canonical) != len(legacy) or len(canonical) != len(provenance):
        raise PublicationError(f"{set_name} 导出记录数量不一致")
    # Only this branch's quality references must be terminal; other branches may
    # still be running or failed when this branch is independently published.
    task_rows = load_jsonl(run_root / "quality" / "stage2_tasks.jsonl")
    result_rows = load_jsonl(run_root / "quality" / "stage2_results.jsonl")
    stage2_tasks = {row.get("cache_key"): row for row in task_rows}
    stage2_results = {row.get("cache_key"): row.get("result") for row in result_rows}
    if (
        None in stage2_tasks or len(stage2_tasks) != len(task_rows)
        or None in stage2_results or len(stage2_results) != len(result_rows)
        or any(row.get("stage") != "stage2" for row in [*task_rows, *result_rows])
    ):
        raise PublicationError("Stage2 任务或结果账本非法")
    quality_contract = _load_object(run_root / "quality" / "quality_run_contract.json")
    stage1_task_rows = load_jsonl(run_root / "quality" / "stage1_tasks.jsonl")
    stage1_result_rows = load_jsonl(run_root / "quality" / "stage1_results.jsonl")
    stage1_tasks = {row.get("cache_key"): row for row in stage1_task_rows}
    stage1_results = {row.get("cache_key"): row.get("result") for row in stage1_result_rows}
    if (None in stage1_tasks or len(stage1_tasks) != len(stage1_task_rows)
            or None in stage1_results or len(stage1_results) != len(stage1_result_rows)
            or any(row.get("stage") != "stage1" for row in [*stage1_task_rows, *stage1_result_rows])):
        raise PublicationError("Stage1 任务或结果账本非法")
    for index, (canonical_row, legacy_row, provenance_row) in enumerate(
        zip(canonical, legacy, provenance), start=1
    ):
        _validate_stage2_binding(canonical_row, stage2_tasks, stage2_results, quality_contract)
        _validate_stage1_binding(canonical_row, stage1_tasks, stage1_results, quality_contract)
        validate_legacy_record(set_name, legacy_row, canonical_record=canonical_row)
        if (
            provenance_row.get("output_index") != index
            or provenance_row.get("canonical_record_sha256") != stable_record_sha256(canonical_row)
            or provenance_row.get("legacy_record_sha256") != stable_record_sha256(legacy_row)
        ):
            raise PublicationError(f"{set_name} 第 {index} 条 provenance 不一致")
    report = _load_object(report_path)
    if report.get("set") != set_name or report.get("records") != len(canonical):
        raise PublicationError(f"{set_name} 分支导出报告不一致")
    branch_report = _load_object(branch_report_path)
    if (
        branch_report.get("report_schema_version") != RUN_REPORT_SCHEMA_VERSION
        or branch_report.get("set") != set_name
        or branch_report.get("status") != "completed"
        or branch_report.get("final") != len(canonical)
        or branch_report.get("export") != report
    ):
        raise PublicationError(f"{set_name} 分支报告不一致")
    evaluated_ids = {str(row["source_identity_sha256"]) for row in load_jsonl(branch_root / "candidate_tasks.jsonl")}
    branch_stage1: dict[str, JsonObject] = {}
    for task_id, task in stage1_tasks.items():
        identities = evaluated_ids.intersection(task.get("source_identities", []))
        if not identities:
            continue
        if task_id not in stage1_results:
            raise PublicationError(f"{set_name} Stage1 候选缺少合法终态")
        result = validate_stage1_result(stage1_results[task_id])
        for identity in identities:
            if identity in branch_stage1 and branch_stage1[identity] != result:
                raise PublicationError(f"{set_name} Stage1 同一候选结果冲突")
            branch_stage1[identity] = result
    if set(branch_stage1) != evaluated_ids:
        raise PublicationError(f"{set_name} Stage1 审计候选无法关联结果")
    audit = branch_report.get("audit", {})
    if (audit != _load_object(branch_root / "branch_audit.json")
            or branch_report.get("evaluated") != len(evaluated_ids)
            or any(audit.get(key) != value for key, value in stage1_issue_summary(list(branch_stage1.values())).items())
            or audit.get("stage2_semantic_review") != stage2_semantic_review([
                {key: row["quality"][key] for key in STAGE2_FIELDS} for row in canonical
            ])):
        raise PublicationError(f"{set_name} 分支质量审计与账本不一致")
    destination = publish_root / LEGACY_FILENAMES[set_name]
    source_hash = sha256_file(legacy_path)
    pending: list[tuple[Path, Path]] = []
    conflict = destination.is_file() and sha256_file(destination) != source_hash
    if conflict:
        _log_content_conflict(set_name, destination, replace=replace)
    if conflict and not replace:
        raise PublicationError(f"{set_name} 发布目标已有不同内容；如确认替换请使用 --replace")
    if not destination.is_file() or conflict:
        pending.append((legacy_path, destination))
    _transactional_publish(pending)
    if not destination.is_file() or sha256_file(destination) != source_hash:
        raise PublicationError(f"{set_name} 发布后文件校验失败")
    return {
        "set": set_name,
        "destination": str(destination),
        "sha256": source_hash,
        "records": len(legacy),
        "replaced": conflict,
    }
