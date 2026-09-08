"""Build policy-specific P0/P1/P2 authoritative screened datasets."""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from .core import (
    SET_FILES,
    JsonObject,
    ensure_within,
    get_decision,
    is_testable_difficulty,
    iter_jsonl,
    load_jsonl,
    prescreen_results_path,
    run_prescreen_dataset_path,
    run_screened_dataset_path,
    run_supplement_dataset_path,
    sample_identity,
    set_workspace,
    supplement_artifact_path,
    validate_selected_sets,
    validate_source_run_roots,
    write_json,
    write_jsonl,
)
from .cluster_index import identity_sha256, read_indexed_record, record_sha256
from .coverage import TaxonomyMappings, load_taxonomy_mappings, stable_take
from .logging_config import setup_logger

logger = logging.getLogger(__name__)


def _load_json_object(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须包含 JSON 对象")
    return value


def _deduplicate_in_order(records: list[JsonObject]) -> list[JsonObject]:
    seen: set[tuple[str, str]] = set()
    output: list[JsonObject] = []
    for record in records:
        identity = sample_identity(record)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(record)
    return output


def _initial_rows(source_root: Path, set_name: str) -> dict[int, JsonObject]:
    return {line: row for line, row in iter_jsonl(source_root / SET_FILES[set_name])}


def _results_for_set(run_root: Path, set_name: str) -> list[JsonObject]:
    path = prescreen_results_path(run_root, set_name)
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {set_name} 统一质检结果: {path}")
    results = load_jsonl(path)
    seen: set[str] = set()
    for result in results:
        filter_id = result.get("filter_id")
        if result.get("set") != set_name or not isinstance(filter_id, str) or filter_id in seen:
            raise ValueError(f"{set_name} 统一质检结果存在非法或重复记录")
        seen.add(filter_id)
    return results


def _selected_initial_records(
    rows: dict[int, JsonObject],
    results: list[JsonObject],
    *,
    include_testable_review: bool,
) -> list[JsonObject]:
    selected_lines: list[int] = []
    for result in results:
        line = result.get("source_line")
        if type(line) is not int or line not in rows:
            raise ValueError("质检结果无法关联原始物理行")
        if get_decision(result) == "KEEP" or (
            include_testable_review and is_testable_difficulty(result)
        ):
            selected_lines.append(line)
    return _deduplicate_in_order([rows[line] for line in sorted(selected_lines)])


def _resolve_l1(row: JsonObject, mappings: TaxonomyMappings) -> str | None:
    cluster_id = row.get("cluster")
    raw_l1 = str(row.get("L1") or "").strip()
    if cluster_id is not None:
        mapped = mappings.cluster_to_l1.get(cluster_id)
        if mapped is None:
            return None
        if raw_l1 and raw_l1 != mapped:
            raise ValueError(f"P1 初版 L1 与 cluster {cluster_id!r} 的 taxonomy 映射冲突")
        return mapped
    return raw_l1 if raw_l1 in mappings.l1_population else None


def _resolve_l2(row: JsonObject, l1: str, mappings: TaxonomyMappings) -> str:
    cluster_id = row.get("cluster")
    if cluster_id is not None:
        return mappings.cluster_to_l2.get(cluster_id, f"{l1}/__L1_ONLY__")
    raw_l2 = str(row.get("L2") or "").strip()
    return f"{l1}/{raw_l2}" if raw_l2 else f"{l1}/__L1_ONLY__"


def _p1_qualified(
    source_root: Path,
    run_root: Path,
    results: list[JsonObject],
    quotas: dict[str, int],
    taxonomy_path: Path,
    upstream_path: Path,
) -> tuple[list[JsonObject], list[JsonObject], list[JsonObject]]:
    mappings = load_taxonomy_mappings(taxonomy_path)
    rows = _initial_rows(source_root, "P1")
    initial: list[JsonObject] = []
    for result in results:
        if get_decision(result) != "KEEP":
            continue
        line = result.get("source_line")
        if type(line) is not int or line not in rows:
            raise ValueError("P1 初筛 KEEP 无法关联原始物理行")
        row = rows[line]
        l1 = _resolve_l1(row, mappings)
        if l1 not in quotas:
            continue
        initial.append(
            {
                "origin": "initial", "source_line": line, "L1": l1,
                "L2": _resolve_l2(row, l1, mappings), "cluster": row.get("cluster"),
                "identity": list(sample_identity(row)), "record": row,
            }
        )

    supplement_results_path = supplement_artifact_path(run_root, "supplement_results.jsonl")
    supplement_results = load_jsonl(supplement_results_path) if supplement_results_path.is_file() else []
    tasks_path = supplement_artifact_path(run_root, "supplement_tasks.jsonl")
    tasks = load_jsonl(tasks_path) if tasks_path.is_file() else []
    tasks_by_id: dict[str, JsonObject] = {}
    for task in tasks:
        filter_id = task.get("filter_id")
        if not isinstance(filter_id, str) or not filter_id or filter_id in tasks_by_id:
            raise ValueError("P1 增量任务存在非法或重复 filter_id")
        tasks_by_id[filter_id] = task
    result_ids: set[str] = set()
    for result in supplement_results:
        filter_id = result.get("filter_id")
        if not isinstance(filter_id, str) or filter_id in result_ids:
            raise ValueError("P1 增量结果存在非法或重复 filter_id")
        task = tasks_by_id.get(filter_id)
        if task is None or any(
            result.get(key) != task.get(key)
            for key in ("set", "source_line", "L1", "L2", "cluster")
        ):
            raise ValueError("P1 增量结果与冻结任务账本不一致")
        result_ids.add(filter_id)
    if result_ids != set(tasks_by_id):
        raise ValueError("P1 增量任务尚未全部形成合法终态")
    keep_results = [item for item in supplement_results if get_decision(item) == "KEEP"]
    supplement: list[JsonObject] = []
    for result in keep_results:
        task = tasks_by_id[str(result["filter_id"])]
        line = int(result["source_line"])
        row = read_indexed_record(upstream_path, task)
        cluster_id = row.get("cluster")
        mapped_l1 = mappings.cluster_to_l1.get(cluster_id)
        result_l1 = str(result.get("L1") or "")
        if mapped_l1 is None or mapped_l1 != result_l1 or result_l1 not in quotas:
            raise ValueError("P1 增量 KEEP 的 cluster/L1 关联非法")
        record = dict(row)
        record["_target_level"] = "l1"
        record["_target_category"] = result_l1
        supplement.append(
            {
                "origin": "upstream", "source_line": line,
                "task_id": task["filter_id"],
                "round_id": result.get("round_id"), "L1": result_l1,
                "L2": str(result.get("L2") or "未知"), "cluster": cluster_id,
                "identity": list(sample_identity(record)), "record": record,
                "identity_sha256": task.get("identity_sha256"),
                "byte_offset": task.get("byte_offset"),
                "byte_length": task.get("byte_length"),
                "source_record_sha256": task.get("record_sha256"),
            }
        )

    initial.sort(key=lambda item: int(item["source_line"]))
    supplement.sort(key=lambda item: int(item["source_line"]))

    unique: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for item in initial + supplement:
        identity = tuple(str(value) for value in item["identity"])
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return unique, initial, supplement


def _select_p1(
    qualified: list[JsonObject], quotas: dict[str, int], seed: int
) -> tuple[list[JsonObject], dict[str, int], dict[str, int], list[JsonObject]]:
    selected_refs: list[JsonObject] = []
    qualified_counts: Counter[str] = Counter()
    selected_counts: Counter[str] = Counter()
    for l1, quota in quotas.items():
        candidates = [item for item in qualified if item["L1"] == l1]
        qualified_counts[l1] = len(candidates)
        chosen = stable_take(candidates, quota, seed=seed, namespace=f"p1-final-{l1}")
        selected_counts[l1] = len(chosen)
        selected_refs.extend(chosen)
    selected_refs.sort(
        key=lambda item: (0 if item["origin"] == "initial" else 1, int(item["source_line"]))
    )
    return (
        [item["record"] for item in selected_refs],
        dict(qualified_counts), dict(selected_counts), selected_refs,
    )


def _category_counts(records: list[JsonObject], field: str) -> Counter[str]:
    return Counter(str(record.get(field) or "未知") for record in records)


def _set_diagnostics(
    all_rows: list[JsonObject], eligible_scores: set[int],
    results: list[JsonObject], final_records: list[JsonObject],
) -> JsonObject:
    eligible = [row for row in all_rows if row.get("quality_overall_score") in eligible_scores]
    decisions = Counter(get_decision(result) for result in results)
    coverage: JsonObject = {}
    for field in ("L1", "L2"):
        before = _category_counts(eligible, field)
        after = _category_counts(final_records, field)
        coverage[field] = {
            "eligible": dict(sorted(before.items())),
            "final": dict(sorted(after.items())),
            "holes": sorted(name for name, count in before.items() if count and not after[name]),
        }
    return {
        "source_records": len(all_rows), "eligible_records": len(eligible),
        "decision_counts": dict(sorted(decisions.items())),
        "final_records": len(final_records), "coverage": coverage,
    }


def build_screened_samples(
    source_root: Path,
    run_root: Path,
    selected_sets: list[str],
    config: JsonObject | None = None,
    *,
    write_combined_summary: bool = True,
) -> JsonObject:
    """Build authoritative outputs after validating every source reference."""
    source_root, run_root = validate_source_run_roots(source_root, run_root)
    policy = config or {
        "p0": {"scores": [2, 3, 4, 5]},
        "p1_coverage": {"scores": [3, 4, 5], "seed": 42,
                         "supplement": {"enabled": True}},
        "p2": {"scores": [3, 4, 5], "accept": ["KEEP", "TESTABLE_DIFFICULTY_REVIEW"]},
    }
    output: dict[str, tuple[list[JsonObject], list[JsonObject] | None, list[JsonObject]]] = {}
    diagnostics: JsonObject = {}
    p1_provenance: list[JsonObject] | None = None

    for set_name in selected_sets:
        rows_by_line = _initial_rows(source_root, set_name)
        results = _results_for_set(run_root, set_name)
        if set_name == "P0":
            prescreen = _selected_initial_records(
                rows_by_line, results, include_testable_review=False
            )
            supplement = None
            screened = prescreen
            scores = set(policy["p0"]["scores"])
        elif set_name == "P2":
            prescreen = _selected_initial_records(
                rows_by_line, results,
                include_testable_review=(
                    "TESTABLE_DIFFICULTY_REVIEW" in policy["p2"]["accept"]
                ),
            )
            supplement = None
            screened = prescreen
            scores = set(policy["p2"]["scores"])
        else:
            summary_path = supplement_artifact_path(run_root, "supplement_summary.json")
            if not summary_path.is_file():
                raise FileNotFoundError(f"缺少 P1 Coverage 摘要: {summary_path}")
            summary = _load_json_object(summary_path)
            quotas = {str(key): int(value) for key, value in summary.get("quota_by_l1", {}).items()}
            inputs = policy.get("inputs", {})
            taxonomy_path = Path(inputs["taxonomy"])
            upstream_path = Path(inputs["upstream_cluster_population"])
            qualified, initial_refs, supplement_refs = _p1_qualified(
                source_root, run_root, results, quotas, taxonomy_path, upstream_path
            )
            prescreen = _deduplicate_in_order([item["record"] for item in initial_refs])
            supplement = _deduplicate_in_order([item["record"] for item in supplement_refs])
            screened, qualified_counts, selected_counts, selected_refs = _select_p1(
                qualified, quotas, int(policy["p1_coverage"]["seed"])
            )
            p1_provenance = []
            for output_index, item in enumerate(selected_refs, start=1):
                provenance: JsonObject = {
                    "output_index": output_index,
                    "origin": item["origin"],
                    "source_line": int(item["source_line"]),
                    "L1": item["L1"],
                    "L2": item["L2"],
                    "cluster": item.get("cluster"),
                    "identity_sha256": (
                        item.get("identity_sha256")
                        or identity_sha256(item["record"])
                    ),
                    "output_record_sha256": record_sha256(item["record"]),
                }
                if item["origin"] == "initial":
                    provenance["source"] = "weekly_labeled"
                    provenance["source_record_sha256"] = record_sha256(item["record"])
                else:
                    provenance.update(
                        {
                            "source": "upstream_cluster",
                            "task_id": item["task_id"],
                            "round_id": item.get("round_id"),
                            "byte_offset": int(item["byte_offset"]),
                            "byte_length": int(item["byte_length"]),
                            "source_record_sha256": item["source_record_sha256"],
                        }
                    )
                p1_provenance.append(provenance)
            summary["final_qualified_by_l1"] = qualified_counts
            summary["final_selected_by_l1"] = selected_counts
            summary["final_selected"] = len(screened)
            summary["final_selected_by_l2"] = dict(sorted(Counter(str(item.get("L2") or "未知") for item in selected_refs).items()))
            summary["final_selected_by_cluster"] = dict(sorted(Counter(str(item["cluster"]) for item in selected_refs if item.get("cluster") is not None).items()))
            summary["surplus_qualified"] = sum(
                max(0, qualified_counts.get(l1, 0) - quotas.get(l1, 0)) for l1 in quotas
            )
            population_l2 = summary.get("population", {}).get("l2_counts", {})
            summary["l2_coverage_holes"] = sorted(
                name for name, count in population_l2.items()
                if int(count) > 0 and not summary["final_selected_by_l2"].get(name, 0)
            )
            write_json(summary_path, summary)
            if not policy["p1_coverage"]["supplement"]["enabled"]:
                supplement = None
            scores = set(policy["p1_coverage"]["scores"])
        output[set_name] = (prescreen, supplement, screened)
        diagnostics[set_name] = _set_diagnostics(
            list(rows_by_line.values()), scores, results, screened
        )

    for set_name, (prescreen, supplement, screened) in output.items():
        write_jsonl(run_prescreen_dataset_path(run_root, set_name), prescreen)
        if supplement is not None:
            write_jsonl(run_supplement_dataset_path(run_root, set_name), supplement)
        write_jsonl(run_screened_dataset_path(run_root, set_name), screened)
        logger.info(
            "%s: prescreen=%d supplement=%s screened=%d",
            set_name, len(prescreen), "disabled" if supplement is None else len(supplement), len(screened),
        )
    if p1_provenance is not None:
        write_jsonl(
            supplement_artifact_path(run_root, "screened_provenance.jsonl"),
            p1_provenance,
        )
    summary = {"schema_version": 3, "sets": diagnostics}
    for set_name, detail in diagnostics.items():
        write_json(
            set_workspace(run_root, set_name) / "screening_summary.json",
            {"schema_version": 3, "set": set_name, "detail": detail},
        )
    if write_combined_summary:
        write_json(run_root / "screening_summary.json", summary)
    return summary


def collect_latest_decisions(
    records: list[JsonObject], selected_sets: set[str], stage_name: str
) -> dict[tuple[str, int], str]:
    latest: dict[tuple[str, int], str] = {}
    for index, record in enumerate(records, start=1):
        set_name = record.get("set")
        line = record.get("source_line")
        if set_name in selected_sets and type(line) is int and line >= 1:
            latest[(str(set_name), line)] = get_decision(record)
        elif get_decision(record) == "KEEP":
            raise ValueError(f"{stage_name} 第 {index} 条 KEEP 缺少合法 set/source_line")
    return latest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 P0/P1/P2 最终 screened 数据集")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--sets", default="P0,P1,P2")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    parser.add_argument("--log-file", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    global logger
    source_root, run_root = validate_source_run_roots(args.source_root, args.run_root)
    log_path = ensure_within(
        args.log_file or run_root / "logs" / "pipeline.log", run_root, "日志文件"
    )
    logger = setup_logger(
        __name__, args.log_level, log_path, run_id=run_root.name, stage="build_screened"
    )
    try:
        if args.config:
            config = _load_json_object(args.config)
        elif (run_root / "manifest.json").is_file():
            manifest = _load_json_object(run_root / "manifest.json")
            value = manifest.get("configuration")
            config = value if isinstance(value, dict) else None
        else:
            config = None
        build_screened_samples(
            source_root, run_root, validate_selected_sets(args.sets), config
        )
        return 0
    except (OSError, ValueError, KeyError) as exc:
        logger.error("生成 screened 数据集失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
