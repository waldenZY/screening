"""Generate the human-readable screening and P1 Coverage report."""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections import Counter
from pathlib import Path

from .core import JsonObject, ensure_within, load_jsonl, set_workspace, supplement_artifact_path, write_text
from .logging_config import setup_logger

logger = logging.getLogger(__name__)


def _format_duration(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    milliseconds = max(0.0, float(value))
    if milliseconds >= 60_000:
        return f"{milliseconds / 60_000:.2f} min"
    if milliseconds >= 1_000:
        return f"{milliseconds / 1_000:.2f} s"
    return f"{milliseconds:.0f} ms"


def _latency_stats(rows: list[JsonObject], field: str) -> JsonObject:
    values = sorted(
        float(row[field])
        for row in rows
        if isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
        and float(row[field]) >= 0
    )
    if not values:
        return {"count": 0, "average_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}

    def nearest_rank(percentile: float) -> float:
        return values[max(0, math.ceil(percentile * len(values)) - 1)]

    return {
        "count": len(values),
        "average_ms": sum(values) / len(values),
        "p50_ms": nearest_rank(0.50),
        "p95_ms": nearest_rank(0.95),
        "max_ms": values[-1],
    }


def _llm_stage_group(stage: str) -> str:
    if stage.startswith("p1_coverage_round_"):
        return "p1_supplement"
    return stage


def _llm_stage_label(stage: str) -> str:
    labels = {
        "p0_prescreen": "P0 prescreen",
        "p1_prescreen": "P1 prescreen",
        "p2_prescreen": "P2 prescreen",
        "p1_supplement": "P1 supplement",
    }
    return labels.get(stage, stage)


def _timing_entry_lines(
    rows: list[tuple[str, JsonObject]],
) -> list[str]:
    lines: list[str] = []
    for label, entry in rows:
        if not isinstance(entry, dict) or "active_duration_ms" not in entry:
            continue
        lines.append(
            f"| {label} | {entry.get('execution_count', 0)} | "
            f"{_format_duration(entry.get('active_duration_ms'))} | "
            f"{_format_duration(entry.get('last_duration_ms'))} | "
            f"{entry.get('last_started_at', '—')} | {entry.get('last_finished_at', '—')} |"
        )
    return lines


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

    manifest_path = run_root / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.is_file() else {}
    timing = manifest.get("timing", {})
    lines.extend(["## 运行时延", ""])
    if not isinstance(timing, dict) or not timing:
        lines.extend(["本次运行没有可用的结构化时延记录。", ""])
    else:
        lines.extend(
            [
                f"- 首次开始：{timing.get('first_started_at', '—')}",
                f"- 完成时间：{timing.get('completed_at', '—')}",
                f"- 自然时间跨度：{_format_duration(timing.get('wall_clock_duration_ms'))}",
                f"- 累计活跃执行耗时：{_format_duration(timing.get('active_duration_ms'))}",
                f"- 启动次数：{timing.get('invocation_count', 0)}；"
                f"断点恢复次数：{timing.get('resume_count', 0)}",
                "",
                "### 主阶段耗时",
                "",
                "| 阶段 | 执行次数 | 累计活跃耗时 | 最近一次耗时 | 最近开始 | 最近结束 |",
                "|---|---:|---:|---:|---|---|",
            ]
        )
        phases = timing.get("phases", {})
        stages = manifest.get("stages", {})
        main_rows = [
            ("初始化与 source 校验", phases.get("initialization", {})),
            ("Cluster 输入审计及索引准备", stages.get("cluster_prepare", {}).get("timing", {})),
            ("Quality Control", stages.get("quality_control", {}).get("timing", {})),
            ("P1 Coverage", stages.get("p1_coverage", {}).get("timing", {})),
            ("汇总构建", stages.get("build_screened", {}).get("timing", {})),
            ("报告生成", stages.get("report", {}).get("timing", {})),
            ("发布与索引清理", stages.get("publish", {}).get("timing", {})),
        ]
        lines.extend(_timing_entry_lines(main_rows))
        lines.extend(
            [
                "",
                "### 分支耗时",
                "",
                "| 分支阶段 | 执行次数 | 累计活跃耗时 | 最近一次耗时 | 最近开始 | 最近结束 |",
                "|---|---:|---:|---:|---|---|",
            ]
        )
        branch_rows: list[tuple[str, JsonObject]] = []
        branches = manifest.get("branches", {})
        for set_name in ("P0", "P1", "P2"):
            branch = branches.get(set_name, {})
            for stage_name, label in (
                ("prescreen", "prescreen"),
                ("population_prepare", "Population 准备"),
                ("supplement", "supplement"),
                ("build", "build"),
            ):
                value = branch.get(stage_name, {})
                branch_rows.append(
                    (f"{set_name} {label}", value.get("timing", {}) if isinstance(value, dict) else {})
                )
        lines.extend(_timing_entry_lines(branch_rows))
        lines.extend(
            [
                "",
                "> 主阶段和分支可能并行或互相包含，耗时不可相加作为整链路总耗时；请以“累计活跃执行耗时”为准。",
                "",
            ]
        )

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
    grouped_attempts: dict[str, list[JsonObject]] = {}
    for attempt in attempts:
        stage = _llm_stage_group(str(attempt.get("stage") or "unknown"))
        grouped_attempts.setdefault(stage, []).append(attempt)
    for title, field in (
        ("首 Token 时延", "first_token_latency_ms"),
        ("完整响应时延", "full_latency_ms"),
    ):
        lines.extend(
            [
                f"### {title}",
                "",
                "| LLM 阶段 | 尝试次数 | 有效时延样本数 | 平均值 | P50 | P95 | 最大值 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for stage in sorted(grouped_attempts):
            rows = grouped_attempts[stage]
            stats = _latency_stats(rows, field)
            lines.append(
                f"| {_llm_stage_label(stage)} | {len(rows)} | {stats['count']} | "
                f"{_format_duration(stats['average_ms'])} | "
                f"{_format_duration(stats['p50_ms'])} | "
                f"{_format_duration(stats['p95_ms'])} | "
                f"{_format_duration(stats['max_ms'])} |"
            )
        if not grouped_attempts:
            lines.append("| 无 | 0 | 0 | — | — | — | — |")
        lines.append("")
    lines.extend([
        "有效时延样本指该字段包含非负数值的 attempt；没有时延的异常 attempt 只计入尝试次数。",
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
