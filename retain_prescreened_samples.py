"""Materialize records accepted by each branch's pre-screening policy.

Run with::

    python -m filter_v2.retain_prescreened_samples \
        --source-root ./source/0803-0809 --run-root ./outputs/run-id
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .core import (
    JsonObject,
    SET_FILES,
    ensure_within,
    get_decision,
    is_testable_difficulty,
    load_jsonl,
    load_labeled_by_line,
    prescreen_results_path,
    run_prescreen_dataset_path,
    validate_selected_sets,
    validate_source_run_roots,
    write_jsonl,
)
from .logging_config import setup_logger
from .coverage import load_taxonomy_mappings

logger = logging.getLogger(__name__)


def collect_keep_records(
    results: list[JsonObject],
    labeled_by_line: dict[int, JsonObject],
) -> tuple[list[JsonObject], int, int]:
    """Resolve the last decision per source line to original labeled records."""
    latest_decisions: dict[int, str] = {}
    missing_count = 0
    duplicate_count = 0

    for result in results:
        decision = get_decision(result)
        source_line = result.get("source_line")
        if type(source_line) is not int or source_line < 1:
            if decision == "KEEP":
                missing_count += 1
            continue
        if source_line in latest_decisions:
            duplicate_count += 1
        latest_decisions[source_line] = decision

    keep_records: list[JsonObject] = []
    for source_line in sorted(latest_decisions):
        if latest_decisions[source_line] != "KEEP":
            continue
        if source_line not in labeled_by_line:
            missing_count += 1
            continue
        keep_records.append(labeled_by_line[source_line])

    return keep_records, missing_count, duplicate_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将统一质检结果关联回原始数据并按集合策略接纳"
    )
    parser.add_argument("--source-root", type=Path, required=True, help="只读原始周目录")
    parser.add_argument("--run-root", type=Path, required=True, help="运行工作目录")
    parser.add_argument(
        "--sets", default="P0,P1,P2", help="需要处理的数据集，逗号分隔"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="日志级别",
    )
    parser.add_argument("--log-file", type=Path, default=None, help="日志文件路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    global logger
    source_root, run_root = validate_source_run_roots(
        args.source_root, args.run_root
    )
    log_path = ensure_within(
        args.log_file or run_root / "logs" / "pipeline.log",
        run_root,
        "日志文件",
    )
    logger = setup_logger(
        __name__,
        args.log_level,
        log_path,
        run_id=run_root.name,
        stage="retain_prescreened",
    )
    selected_sets = validate_selected_sets(args.sets)

    return retain_prescreened_samples(source_root, run_root, selected_sets)


def retain_prescreened_samples(
    source_root: Path,
    run_root: Path,
    selected_sets: list[str],
) -> int:
    """Materialize per-set prescreen datasets inside the run workspace."""
    source_root, run_root = validate_source_run_roots(source_root, run_root)
    output_records: dict[str, list[JsonObject]] = {}
    mappings = None
    if "P1" in selected_sets:
        manifest_path = run_root / "manifest.json"
        if not manifest_path.is_file():
            logger.error("P1 初筛物化需要 manifest 中的 taxonomy 配置")
            return 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        taxonomy = manifest.get("configuration", {}).get("inputs", {}).get("taxonomy")
        if not taxonomy:
            logger.error("manifest 缺少 taxonomy 配置")
            return 1
        mappings = load_taxonomy_mappings(Path(taxonomy))
    for set_name in selected_sets:
        results_path = prescreen_results_path(run_root, set_name)
        if not results_path.is_file():
            logger.error("结果文件不存在: %s", results_path)
            return 1
        results = load_jsonl(results_path)
        labeled_path = source_root / SET_FILES[set_name]
        if not labeled_path.is_file():
            logger.error("%s: labeled 文件不存在: %s", set_name, labeled_path)
            return 1

        labeled = load_labeled_by_line(labeled_path)
        selected_lines: list[int] = []
        duplicate_count = 0
        seen_lines: set[int] = set()
        missing_count = 0
        for result in results:
            line = result.get("source_line")
            accepted = get_decision(result) == "KEEP" or (
                set_name == "P2" and is_testable_difficulty(result)
            )
            if not accepted:
                continue
            if type(line) is not int or line not in labeled:
                missing_count += 1
                continue
            if line in seen_lines:
                duplicate_count += 1
                continue
            if set_name == "P1" and mappings is not None:
                row = labeled[line]
                cluster_id = row.get("cluster")
                raw_l1 = str(row.get("L1") or "").strip()
                if cluster_id is not None:
                    mapped_l1 = mappings.cluster_to_l1.get(cluster_id)
                    if mapped_l1 is None:
                        continue
                    if raw_l1 and raw_l1 != mapped_l1:
                        logger.error("P1 初版 L1 与 taxonomy 映射冲突: line=%d", line)
                        return 1
                elif raw_l1 not in mappings.l1_population:
                    continue
            seen_lines.add(line)
            selected_lines.append(line)
        keep_records = [labeled[line] for line in sorted(selected_lines)]
        logger.info(
            "%s: accepted=%d, missing=%d, duplicate=%d",
            set_name,
            len(keep_records),
            missing_count,
            duplicate_count,
        )
        if missing_count:
            logger.error("%s: 有 KEEP 结果无法关联原始记录", set_name)
            return 1
        output_records[set_name] = keep_records

    for set_name in selected_sets:
        output_path = run_prescreen_dataset_path(run_root, set_name)
        write_jsonl(output_path, output_records[set_name])
        logger.info("%s: 输出 %s", set_name, output_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
