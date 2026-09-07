"""Generate the human-readable screening and P1 Coverage report."""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from .core import JsonObject, ensure_within, load_jsonl, set_workspace, supplement_artifact_path, write_text
from .shared import setup_logger

logger = logging.getLogger(__name__)


def load_json(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须包含 JSON 对象")
    return value


def _coverage_lines(set_name: str, detail: JsonObject) -> list[str]:
    lines = [f"### {set_name} 覆盖空洞", ""]
    coverage = detail.get("coverage", {})
    found = False
    for level in ("L1", "L2"):
        holes = coverage.get(level, {}).get("holes", [])
        if holes:
            found = True
            lines.append(f"- {level}：{', '.join(str(item) for item in holes)}")
    if not found:
        lines.append("- 未发现“有合格分数输入但最终为零”的类别。")
    lines.append("")
    return lines


def generate_report(run_root: Path) -> str:
    screening_path = run_root / "screening_summary.json"
    p1_path = supplement_artifact_path(run_root, "supplement_summary.json")
    if not screening_path.is_file():
        raise FileNotFoundError("缺少 screening_summary.json")
    screening = load_json(screening_path)
    p1 = load_json(p1_path) if p1_path.is_file() else {"enabled": False}
    sets = screening.get("sets", {})
    lines = ["# 分集合筛选与 P1 Coverage 报告", ""]

    lines.extend([
        "## 最终结果",
        "",
        "| 集合 | 原始记录 | 进入质检 | KEEP | REVIEW | REJECT | 最终保留 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for set_name, detail in sets.items():
        decisions = detail.get("decision_counts", {})
        lines.append(
            f"| {set_name} | {detail.get('source_records', 0)} | "
            f"{detail.get('eligible_records', 0)} | {decisions.get('KEEP', 0)} | "
            f"{decisions.get('REVIEW', 0)} | {decisions.get('REJECT', 0)} | "
            f"{detail.get('final_records', 0)} |"
        )
    lines.append("")

    lines.extend(["## P0/P2 覆盖诊断", ""])
    for set_name in ("P0", "P2"):
        if set_name in sets:
            lines.extend(_coverage_lines(set_name, sets[set_name]))

    lines.extend(["## P1 目标分布与最终缺口", ""])
    if not p1.get("enabled", True):
        lines.append("本次运行未选择 P1。")
        lines.append("")
    else:
        params = p1.get("params", {})
        population = p1.get("population", {})
        lines.extend([
            f"- 计数方式：{population.get('count_mode', 'taxonomy_total_size')}",
            f"- alpha：{params.get('alpha')}",
            f"- target_n：{params.get('target_n')}",
            f"- 映射成员：{population.get('mapped_rows', 0)}",
            f"- cluster 原始行数：{population.get('total_rows', 0)}；"
            f"相对 taxonomy 总量差：{population.get('row_count_vs_taxonomy_delta', 0)}",
            f"- 未映射并排除：{population.get('unmapped_rows', 0)}",
            f"- 去重排除：{population.get('duplicate_rows', 0)}",
            f"- 噪音簇记录：{population.get('noise_rows', 0)}",
            f"- frequency 诊断总和：{population.get('frequency_sum', 0)}（不参与配额）",
            f"- frequency 相对原始行数差："
            f"{population.get('frequency_vs_row_count_delta', 0)}；"
            f"相对 taxonomy 总量差：{population.get('frequency_vs_taxonomy_delta', 0)}",
            f"- 非法 frequency 记录：{population.get('invalid_frequency_rows', 0)}",
            f"- pct 重算差异类别：{len(population.get('pct_differences', {}))}",
            "",
            "| L1 | Population | 目标占比 | 配额 | 初始 KEEP | 最终合格 | 最终入选 | 剩余缺口 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        population_l1 = population.get("l1_counts", {})
        shares = p1.get("target_share", {})
        quotas = p1.get("quota_by_l1", {})
        initial = p1.get("initial_qualified_by_l1", {})
        final = p1.get("final_qualified_by_l1", {})
        selected = p1.get("final_selected_by_l1", {})
        unresolved = p1.get("final_deficit_by_l1", p1.get("final_unresolved", {}))
        for l1 in sorted(quotas):
            lines.append(
                f"| {l1} | {population_l1.get(l1, 0)} | {shares.get(l1, 0) * 100:.2f}% | "
                f"{quotas.get(l1, 0)} | {initial.get(l1, 0)} | {final.get(l1, 0)} | "
                f"{selected.get(l1, 0)} | {unresolved.get(l1, 0)} |"
            )
        lines.extend([
            "",
            f"停止原因：`{p1.get('stop_reason', 'unknown')}`；"
            f"初始缺口 {p1.get('total_deficit', 0)}，已填补 {p1.get('total_filled', 0)}，"
            f"剩余 {sum(int(value) for value in p1.get('final_unresolved', {}).values())}。",
            "",
            "### 增量轮次",
            "",
            "| 轮次 | 状态 | 候选 | 合法终态 | KEEP | 分类通过率 |",
            "|---:|---|---:|---:|---:|---|",
        ])
        for round_info in p1.get("rounds", []):
            lines.append(
                f"| {round_info.get('round_id')} | {round_info.get('status')} | "
                f"{round_info.get('candidates', 0)} | {round_info.get('terminal', 0)} | "
                f"{round_info.get('keep', 0)} | "
                f"{json.dumps(round_info.get('estimated_pass_rate_by_l1', {}), ensure_ascii=False)} |"
            )
        lines.append("")
        lines.extend(["### L2 软覆盖与 Cluster 诊断", ""])
        l2_population = population.get("l2_counts", {})
        l2_selected = p1.get("final_selected_by_l2", {})
        lines.extend([
            "| L2 | Population | 最终入选 |",
            "|---|---:|---:|",
        ])
        for l2 in sorted(l2_population):
            lines.append(
                f"| {l2} | {l2_population.get(l2, 0)} | {l2_selected.get(l2, 0)} |"
            )
        holes = p1.get("l2_coverage_holes", [])
        lines.append("")
        lines.append(
            "L2 覆盖空洞：" + (", ".join(str(item) for item in holes) if holes else "无")
        )
        lines.append("")
        cluster_population = population.get("cluster_counts", {})
        cluster_selected = p1.get("final_selected_by_cluster", {})
        lines.extend([
            "| Cluster | cluster 文件去重行数 | 最终入选 |",
            "|---|---:|---:|",
        ])
        for cluster in sorted(cluster_population):
            lines.append(
                f"| {cluster} | {cluster_population.get(cluster, 0)} | "
                f"{cluster_selected.get(cluster, 0)} |"
            )
        lines.append("")

    attempts = []
    attempt_paths = [
        set_workspace(run_root, set_name) / "prescreen_attempts.jsonl"
        for set_name in sets
    ]
    attempt_paths.append(supplement_artifact_path(run_root, "supplement_attempts.jsonl"))
    for path in attempt_paths:
        if path.is_file():
            attempts.extend(load_jsonl(path))
    outcomes = Counter(str(row.get("outcome", "unknown")) for row in attempts)
    lines.extend([
        "## 模型调用审计",
        "",
        f"- 调用尝试总数：{len(attempts)}",
        f"- 合法终态响应：{outcomes.get('success', 0)}",
        f"- 解析/结构失败：{outcomes.get('parse_fail', 0)}",
        f"- 调用工程失败：{outcomes.get('call_error', 0)}",
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成筛选与 P1 Coverage 报告")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    parser.add_argument("--log-file", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_root = args.run_root.expanduser().resolve()
    log_path = ensure_within(args.log_file or run_root / "logs" / "pipeline.log", run_root, "日志文件")
    active_logger = setup_logger(__name__, args.log_level, log_path, run_id=run_root.name, stage="report")
    try:
        output = run_root / "supplement_report.md"
        write_text(output, generate_report(run_root))
        active_logger.info("报告已生成: %s", output)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        active_logger.error("报告生成失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
