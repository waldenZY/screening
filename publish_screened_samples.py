"""Validate and atomically publish authoritative screened datasets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import defaultdict, deque
from pathlib import Path

from .core import (
    SCREENED_FILES,
    SET_FILES,
    JsonObject,
    ensure_within,
    get_decision,
    iter_jsonl,
    load_jsonl,
    record_fingerprint,
    prescreen_results_path,
    run_screened_dataset_path,
    sample_identity,
    sha256_file,
    validate_qc_result,
    validate_selected_sets,
    validate_source_run_roots,
    supplement_artifact_path,
)
from .coverage import load_taxonomy_mappings
from .cluster_index import (
    cleanup_cluster_index,
    identity_sha256,
    read_indexed_record,
    record_sha256,
)
from .run_context import (
    cluster_index_handle_from_manifest,
    record_cluster_index_state,
    update_manifest,
    utc_now,
    validate_source_unchanged,
)
from .shared import setup_logger


def _load_manifest(run_root: Path) -> JsonObject:
    with (run_root / "manifest.json").open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    return value


def _validate_source_subsequence(source_path: Path, screened_path: Path) -> int:
    positions: dict[str, deque[int]] = defaultdict(deque)
    for line, row in iter_jsonl(source_path):
        positions[record_fingerprint(row)].append(line)
    previous = 0
    seen_identities: set[tuple[str, str]] = set()
    count = 0
    for row in load_jsonl(screened_path):
        identity = sample_identity(row)
        if identity in seen_identities:
            raise ValueError(f"screened 存在重复样本: {screened_path}")
        seen_identities.add(identity)
        candidates = positions.get(record_fingerprint(row))
        while candidates and candidates[0] <= previous:
            candidates.popleft()
        if not candidates:
            raise ValueError(f"screened 记录无法按原始顺序关联: {screened_path}")
        previous = candidates.popleft()
        count += 1
    return count


def _validate_p1_mixed(
    source_path: Path,
    upstream_path: Path,
    taxonomy_path: Path,
    screened_path: Path,
    provenance_path: Path,
) -> tuple[int, JsonObject]:
    initial_rows = {line: row for line, row in iter_jsonl(source_path)}
    mappings = load_taxonomy_mappings(taxonomy_path)
    screened = load_jsonl(screened_path)
    provenance = load_jsonl(provenance_path)
    tasks_path = provenance_path.with_name("supplement_tasks.jsonl")
    tasks = load_jsonl(tasks_path) if tasks_path.is_file() else []
    tasks_by_id = {str(task.get("filter_id")): task for task in tasks}
    if len(tasks_by_id) != len(tasks) or "None" in tasks_by_id:
        raise ValueError("P1 补样任务账本存在非法或重复 task ID")
    if len(screened) != len(provenance):
        raise ValueError("P1 screened 与来源账本记录数不一致")
    phase = 0
    previous = {0: 0, 1: 0}
    seen: set[tuple[str, str]] = set()
    seen_tasks: set[str] = set()
    for index, (row, source) in enumerate(zip(screened, provenance), start=1):
        if source.get("output_index") != index:
            raise ValueError("P1 来源账本 output_index 不连续")
        identity = sample_identity(row)
        if identity in seen:
            raise ValueError("P1 screened 存在重复样本")
        seen.add(identity)
        if source.get("identity_sha256") != identity_sha256(row):
            raise ValueError("P1 来源账本身份哈希与输出不一致")
        if source.get("output_record_sha256") != record_sha256(row):
            raise ValueError("P1 来源账本输出指纹不一致")
        line = source.get("source_line")
        if type(line) is not int or line < 1:
            raise ValueError("P1 来源账本物理行非法")
        if source.get("origin") == "initial":
            if phase != 0 or line <= previous[0] or line not in initial_rows:
                raise ValueError("P1 初版来源无法关联或出现在上游来源之后")
            original = initial_rows[line]
            if record_sha256(original) != source.get("source_record_sha256"):
                raise ValueError("P1 初版来源记录指纹不一致")
            if record_fingerprint(original) != record_fingerprint(row):
                raise ValueError("P1 初版来源与 screened 内容不一致")
            previous[0] = line
        elif source.get("origin") == "upstream":
            phase = 1
            if line <= previous[1]:
                raise ValueError("P1 上游来源未按物理行升序输出")
            task_id = source.get("task_id")
            if not isinstance(task_id, str) or task_id in seen_tasks:
                raise ValueError("P1 来源账本 task_id 非法或重复")
            task = tasks_by_id.get(task_id)
            if task is None or any(
                source.get(key) != task.get(task_key)
                for key, task_key in (
                    ("source_line", "source_line"),
                    ("byte_offset", "byte_offset"),
                    ("byte_length", "byte_length"),
                    ("identity_sha256", "identity_sha256"),
                    ("source_record_sha256", "record_sha256"),
                    ("L1", "L1"),
                    ("L2", "L2"),
                    ("cluster", "cluster"),
                )
            ):
                raise ValueError("P1 来源账本与补样任务账本不一致")
            original = read_indexed_record(upstream_path, source)
            if record_sha256(original) != source.get("source_record_sha256"):
                raise ValueError("P1 上游来源记录指纹不一致")
            l1 = mappings.cluster_to_l1.get(original.get("cluster"))
            if l1 is None or l1 != source.get("L1"):
                raise ValueError("P1 上游来源的 cluster/L1 映射不一致")
            annotated = dict(original)
            annotated["_target_level"] = "l1"
            annotated["_target_category"] = l1
            if record_fingerprint(annotated) != record_fingerprint(row):
                raise ValueError("P1 上游来源与 screened 内容不一致")
            seen_tasks.add(task_id)
            previous[1] = line
        else:
            raise ValueError("P1 来源账本 origin 非法")
    return len(screened), {
        "path": provenance_path.relative_to(screened_path.parents[1]).as_posix(),
        "sha256": sha256_file(provenance_path),
        "records": len(provenance),
    }


def _validate_canonical(path: Path, expected_sets: set[str]) -> set[str]:
    if not path.is_file():
        raise FileNotFoundError(f"缺少规范结果: {path}")
    ids: set[str] = set()
    for index, row in enumerate(load_jsonl(path), start=1):
        filter_id = row.get("filter_id")
        if not isinstance(filter_id, str) or not filter_id or filter_id in ids:
            raise ValueError(f"{path.name} 第 {index} 条 filter_id 非法或重复")
        if row.get("set") not in expected_sets or type(row.get("source_line")) is not int:
            raise ValueError(f"{path.name} 第 {index} 条来源非法")
        valid, normalized, _ = validate_qc_result(row.get("parsed_result"))
        if not valid or normalized is None or get_decision(row) not in {"KEEP", "REVIEW", "REJECT"}:
            raise ValueError(f"{path.name} 第 {index} 条不是合法质检终态")
        ids.add(filter_id)
    return ids


def screened_artifact_metadata(
    source_root: Path,
    run_root: Path,
    selected_sets: list[str],
) -> JsonObject:
    manifest = _load_manifest(run_root)
    files: JsonObject = {}
    for set_name in selected_sets:
        screened = run_screened_dataset_path(run_root, set_name)
        if not screened.is_file():
            raise FileNotFoundError(f"缺少权威 screened 文件: {screened}")
        if set_name == "P1":
            inputs = manifest.get("inputs", {})
            provenance_path = supplement_artifact_path(run_root, "screened_provenance.jsonl")
            if not provenance_path.is_file():
                raise FileNotFoundError(f"缺少 P1 来源账本: {provenance_path}")
            records, provenance = _validate_p1_mixed(
                source_root / SET_FILES[set_name],
                Path(inputs["upstream_cluster_population"]["path"]),
                Path(inputs["taxonomy"]["path"]),
                screened,
                provenance_path,
            )
        else:
            records = _validate_source_subsequence(source_root / SET_FILES[set_name], screened)
        files[set_name] = {
            "path": screened.relative_to(run_root).as_posix(),
            "sha256": sha256_file(screened),
            "records": records,
        }
        if set_name == "P1":
            files[set_name]["provenance"] = provenance
    return files


def validate_authoritative_screened(
    source_root: Path, run_root: Path, selected_sets: list[str]
) -> JsonObject:
    manifest = _load_manifest(run_root)
    expected = manifest.get("stages", {}).get("build_screened", {}).get("files")
    if not isinstance(expected, dict):
        raise ValueError("manifest 未记录权威 screened 文件哈希")
    current = screened_artifact_metadata(source_root, run_root, selected_sets)
    if current != expected:
        raise ValueError("run 内权威 screened 文件缺失或已被修改")
    return current


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle)
        if sha256_file(source) != sha256_file(temporary):
            raise OSError(f"发布临时文件校验失败: {temporary}")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _transactional_publish(files: list[tuple[Path, Path]]) -> None:
    """Stage every changed file, then commit all destinations with rollback."""
    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for source, destination in files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".publish.tmp",
            )
            temporary = Path(temporary_name)
            with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(source_handle, target)
            if sha256_file(source) != sha256_file(temporary):
                raise OSError(f"发布临时文件校验失败: {temporary}")
            staged.append((temporary, destination))

        for temporary, destination in staged:
            backup: Path | None = None
            if destination.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".backup.tmp",
                )
                os.close(descriptor)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(destination, backup)
            backups[destination] = backup
            os.replace(temporary, destination)
            committed.append(destination)
    except Exception:
        for destination in reversed(committed):
            backup = backups.get(destination)
            destination.unlink(missing_ok=True)
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


def publish_screened_samples(
    source_root: Path,
    run_root: Path,
    selected_sets: list[str] | None = None,
    *,
    publish_root: Path | None = None,
    verify_full_cluster_hash: bool = False,
) -> JsonObject:
    manifest = _load_manifest(run_root)
    source_root, run_root = validate_source_run_roots(source_root, run_root)
    destination_root = (publish_root or source_root).expanduser().resolve()
    if destination_root == run_root or destination_root.is_relative_to(run_root):
        raise ValueError("publish_root 不能等于或位于 run_root 内部")
    if Path(manifest["source"]["source_root"]).resolve() != source_root:
        raise ValueError("发布源目录与 manifest 不一致")
    manifest_sets = [str(value) for value in manifest["configuration"]["common"]["sets"]]
    if selected_sets is None:
        selected_sets = manifest_sets
    elif selected_sets != manifest_sets:
        raise ValueError("发布集合必须与 manifest 完整集合一致")
    validate_source_unchanged(
        run_root, verify_full_cluster_hash=verify_full_cluster_hash
    )
    required = ("quality_control", "p1_coverage", "build_screened", "report")
    incomplete = [name for name in required if manifest.get("stages", {}).get(name, {}).get("status") != "completed"]
    if (
        "P1" in selected_sets
        and manifest.get("stages", {}).get("cluster_prepare", {}).get("status")
        != "completed"
    ):
        incomplete.insert(0, "cluster_prepare")
    if incomplete:
        raise ValueError(f"以下阶段未完成，禁止发布: {', '.join(incomplete)}")
    qc_stage = manifest["stages"]["quality_control"]
    if qc_stage.get("expected") != qc_stage.get("terminal") or qc_stage.get("engineering_failures"):
        raise ValueError("统一质检未完整结束，禁止发布")
    initial_ids: set[str] = set()
    for set_name in selected_sets:
        initial_ids.update(_validate_canonical(prescreen_results_path(run_root, set_name), {set_name}))
    if len(initial_ids) != qc_stage.get("expected"):
        raise ValueError("统一质检结果数量与 manifest 不一致")
    tasks_path = supplement_artifact_path(run_root, "supplement_tasks.jsonl")
    tasks = load_jsonl(tasks_path) if tasks_path.is_file() else []
    task_ids = {str(row.get("filter_id")) for row in tasks}
    if len(task_ids) != len(tasks) or "None" in task_ids:
        raise ValueError("P1 增量任务存在非法或重复 filter_id")
    if "P1" in selected_sets:
        supplement_results = supplement_artifact_path(run_root, "supplement_results.jsonl")
        supplement_ids = _validate_canonical(supplement_results, {"P1"})
        if supplement_ids != task_ids or manifest["stages"]["p1_coverage"].get("engineering_failures"):
            raise ValueError("P1 增量质检未完整结束，禁止发布")
    authoritative = validate_authoritative_screened(source_root, run_root, selected_sets)

    files: JsonObject = {}
    pending_copies: list[tuple[Path, Path]] = []
    for set_name in selected_sets:
        source = run_screened_dataset_path(run_root, set_name)
        destination = destination_root / SCREENED_FILES[set_name]
        if not destination.is_file() or sha256_file(destination) != authoritative[set_name]["sha256"]:
            pending_copies.append((source, destination))
        files[set_name] = {
            "source": source.relative_to(run_root).as_posix(),
            "destination": str(destination),
            "sha256": authoritative[set_name]["sha256"],
            "records": authoritative[set_name]["records"],
        }
    _transactional_publish(pending_copies)
    return {"published_at": utc_now(), "publish_root": str(destination_root), "files": files}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发布已完成 run 的 screened 数据集")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--publish-root", type=Path, default=None)
    parser.add_argument("--sets", default=None)
    parser.add_argument("--verify-full-cluster-hash", action="store_true")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    parser.add_argument("--log-file", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root, run_root = validate_source_run_roots(args.source_root, args.run_root)
    log_path = ensure_within(args.log_file or run_root / "logs" / "pipeline.log", run_root, "日志文件")
    active_logger = setup_logger(__name__, args.log_level, log_path, run_id=run_root.name, stage="publish")
    try:
        publication = publish_screened_samples(
            source_root,
            run_root,
            validate_selected_sets(args.sets) if args.sets else None,
            publish_root=args.publish_root,
            verify_full_cluster_hash=args.verify_full_cluster_hash,
        )
        update_manifest(run_root, status="published", stage="publish", stage_status="published", publication=publication)
        manifest = _load_manifest(run_root)
        if (
            "P1" in manifest["configuration"]["common"]["sets"]
            and manifest["configuration"].get("large_inputs", {}).get(
                "cleanup_index_after_publish", True
            )
        ):
            try:
                cleanup = cleanup_cluster_index(cluster_index_handle_from_manifest(run_root))
                record_cluster_index_state(
                    run_root, cluster_index_handle_from_manifest(run_root)
                )
                update_manifest(
                    run_root,
                    stage="cluster_prepare",
                    stage_status="completed",
                    stage_details={"cleanup_status": cleanup.get("cleanup_status")},
                )
                if cleanup.get("cleanup_status") != "completed":
                    active_logger.warning("cluster 索引清理未完成，将保留 cleanup_pending")
            except (OSError, ValueError, KeyError) as exc:
                active_logger.warning("screened 已发布，但 cluster 索引清理记账失败: %s", exc)
        active_logger.info("screened 数据集发布完成")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        if (run_root / "manifest.json").is_file():
            update_manifest(run_root, status="publish_failed", stage="publish", stage_status="publish_failed", stage_details={"error": str(exc)})
        active_logger.error("发布失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
