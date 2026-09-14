"""Strict artifact validation and transactional publication."""

from __future__ import annotations

import json
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
from ..shared.json_io import JsonObject, load_jsonl, sha256_file
from .legacy import (
    LEGACY_FILENAMES,
    LEGACY_MAPPING_VERSION,
    map_screened_to_legacy_unchecked,
    validate_legacy_record,
)
from .report import (
    RUN_REPORT_JSON,
    RUN_REPORT_MARKDOWN,
    RunReportError,
    load_and_validate_run_report,
)
from .screened import (
    stable_record_sha256,
    validate_screened_canonical_records,
)
from ..quality.two_stage import (
    STAGE1_FIELDS,
    STAGE2_FIELDS,
    stage1_passes,
    stage2_consistency_conflicts,
    validate_stage1_result,
    validate_stage2_result,
)


class PublicationError(ValueError):
    """A v2 artifact or safe-publication contract violation."""


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
) -> tuple[dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject], dict[str, JsonObject]]:
    quality_root = run_root / "quality"
    stage1_task_rows = load_jsonl(quality_root / "stage1_tasks.jsonl")
    stage2_task_rows = load_jsonl(quality_root / "stage2_tasks.jsonl")
    stage1_result_rows = load_jsonl(quality_root / "stage1_results.jsonl")
    stage2_result_rows = load_jsonl(quality_root / "stage2_results.jsonl")
    stage1_tasks = {row.get("cache_key"): row for row in stage1_task_rows}
    stage2_tasks = {row.get("cache_key"): row for row in stage2_task_rows}
    stage1_results = {row.get("cache_key"): row.get("result") for row in stage1_result_rows}
    stage2_results = {row.get("cache_key"): row.get("result") for row in stage2_result_rows}
    if None in stage1_tasks or len(stage1_tasks) != len(stage1_task_rows):
        raise PublicationError("第一阶段任务账本存在重复 ID")
    if None in stage2_tasks or len(stage2_tasks) != len(stage2_task_rows):
        raise PublicationError("第二阶段任务账本存在重复 ID")
    if None in stage1_results or len(stage1_results) != len(stage1_result_rows):
        raise PublicationError("第一阶段结果账本存在非法或重复 ID")
    if None in stage2_results or len(stage2_results) != len(stage2_result_rows):
        raise PublicationError("第二阶段结果账本存在非法或重复 ID")
    if any(row.get("stage") != "stage1" for row in [*stage1_task_rows, *stage1_result_rows]):
        raise PublicationError("第一阶段账本 stage 标记非法")
    if any(row.get("stage") != "stage2" for row in [*stage2_task_rows, *stage2_result_rows]):
        raise PublicationError("第二阶段账本 stage 标记非法")
    for result in stage1_results.values():
        validate_stage1_result(result)
    return stage1_tasks, stage2_tasks, stage1_results, stage2_results


def collect_and_validate_artifacts(run_root: Path) -> JsonObject:
    """Independently validate every authoritative and compatibility artifact."""
    run_root = run_root.expanduser().resolve()
    summary = _load_object(run_root / "p012_build_summary.json")
    aggregate_export = _load_object(run_root / "legacy_export_report.json")
    if aggregate_export.get("legacy_mapping_version") != LEGACY_MAPPING_VERSION:
        raise PublicationError("legacy mapping version 不一致")
    stage1_tasks, stage2_tasks, stage1_results, stage2_results = _task_maps(run_root)
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
    consistency_rows = load_jsonl(run_root / "quality" / "stage2_consistency.jsonl")
    consistency_by_key = {row.get("cache_key"): row for row in consistency_rows}
    if None in consistency_by_key or len(consistency_by_key) != len(consistency_rows):
        raise PublicationError("第二阶段一致性账本存在非法或重复 ID")
    if set(consistency_by_key) != set(stage2_tasks):
        raise PublicationError("第二阶段一致性账本与任务账本不一致")
    if set(stage2_results) != set(stage2_tasks):
        raise PublicationError("第二阶段任务缺少合法终态")
    for task_id, task in stage2_tasks.items():
        identities = [str(value) for value in task.get("source_identities", [])]
        if not identities or any(identity not in stage1_by_identity for identity in identities):
            raise PublicationError("第二阶段一致性账本无法关联第一阶段结果")
        stage1_value = stage1_by_identity[identities[0]]
        if any(stage1_by_identity[identity] != stage1_value for identity in identities):
            raise PublicationError("第二阶段任务对应的第一阶段结果不一致")
        conflicts = list(stage2_consistency_conflicts(stage2_results[task_id], stage1_value))
        expected_consistency = {
            "cache_key": task_id,
            "stage": "stage2",
            "source_identities": sorted(identities),
            "is_consistent": not conflicts,
            "conflict_codes": conflicts,
        }
        if consistency_by_key[task_id] != expected_consistency:
            raise PublicationError("第二阶段一致性账本内容不一致")
    files: JsonObject = {}
    candidate_rows_by_set: dict[str, list[JsonObject]] = {}
    final_l2_counts_by_set: dict[str, Counter[str]] = {}

    for set_name in ("P0", "P1", "P2"):
        branch_root = run_root / set_name
        canonical_path = branch_root / "screened.canonical.jsonl"
        legacy_path = branch_root / LEGACY_FILENAMES[set_name].name
        provenance_path = branch_root / "export_provenance.jsonl"
        candidate_path = branch_root / "candidate_tasks.jsonl"
        branch_report_path = branch_root / "legacy_export_report.json"
        canonical = load_jsonl(canonical_path)
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
            expected_stage2 = {key: canonical_row["quality"][key] for key in STAGE2_FIELDS}
            stage1_value = validate_stage1_result(stage1_results[selection["stage1_task_id"]])
            stage2_value = validate_stage2_result(
                stage2_results[selection["stage2_task_id"]], stage1_value
            )
            if stage1_value != expected_stage1 or stage2_value != expected_stage2:
                raise PublicationError(f"{set_name} 第 {index} 条质量标签与结果账本不一致")
            l2_counts[candidate["taxonomy"]["l2_key"]] += 1
        final_l2_counts_by_set[set_name] = l2_counts

        if set_name != "P1" and any(
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

        report = _load_object(branch_report_path)
        if (
            report.get("set") != set_name
            or report.get("records") != len(canonical)
            or report.get("field_count") != 28
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

    root_files = {
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
            )
            if is_usable:
                usable += 1
                if result["category_fit"] == "exact":
                    exact_usable += 1
            if stage1_passes(result):
                passed += 1
            candidate_l2[str(task["l2_key"])] += 1
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
        "stage1_results.jsonl", "stage2_tasks.jsonl", "stage2_attempts.jsonl",
        "stage2_results.jsonl", "stage2_consistency.jsonl",
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
    for stage in ("stage1", "stage2"):
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
    """Publish only the three labeled files after full v2 validation."""
    run_root = run_root.expanduser().resolve()
    publish_root = publish_root.expanduser().resolve()
    if manifest.get("pipeline_version") != 2:
        raise PublicationError("仅允许发布 screening v2 run")
    required_stages = (
        "canonical_index", "p012_build", "quality_stage1", "p1_supplement",
        "quality_stage2", "screened_export", "batch_report",
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
