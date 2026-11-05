"""Derived delivery reports and slices. All counts refer to delivered rows."""
from collections import Counter
from pathlib import Path
from typing import Any
import json

from ..pipeline.layout import external_root
from ..shared.json_io import load_jsonl, write_json, write_jsonl
from .labeled import LABELED_FILENAMES, QUALITY_EXPORT_FIELDS, base_filename


SLICES = {
    "完美集_score5.jsonl": lambda r: r["quality_overall_score"] == 5,
    "纯净集_score_ge4.jsonl": lambda r: r["quality_overall_score"] >= 4,
    "待审核集_score_le2.jsonl": lambda r: r["quality_overall_score"] <= 2,
    "invalid_无效样本.jsonl": lambda r: r["quality_validity"] == "invalid",
    "mismatch_分类错误.jsonl": lambda r: r["quality_category_fit"] == "mismatch",
    "vague_意图模糊.jsonl": lambda r: r["quality_intent_clarity"] == "vague",
    "no_intent_无意图.jsonl": lambda r: r["quality_intent_clarity"] == "no_intent",
    "fragments_碎片文本.jsonl": lambda r: r["quality_semantic_completeness"] == "fragments",
    "hard_classification_分类难题.jsonl": lambda r: r["quality_classification_difficulty"] == "hard",
    "hard_task_执行难题.jsonl": lambda r: r["quality_task_difficulty"] in {"complex", "extreme"},
}

FIELD_GUIDE = """# 交付字段标准与使用说明

基础版与 labeled 逐行对齐；labeled 只追加八个 quality 字段。
intent_label、executable 保留现有程序规则；executable=0 或质量低分不删行。
text 是完整规范化会话，不可用 target_query 替代多轮输入。

quality_category_fit 复用 Stage1 类别核验。
quality_semantic_completeness：direct=self_contained，clarification=context_dependent。
这里的 context_dependent 表示需要澄清，与 quality_issue_types 同名问题判据不同，不要求同时出现。
quality_classification_difficulty：high=trivial，mid=moderate，boundary=hard，是按聚类概率档的估计。
quality_task_difficulty：直接复用 simple/moderate/complex 规则复杂度，是规则估计。
quality_validity、quality_intent_clarity、quality_issue_types、quality_overall_score 使用现有四字段 LLM 标准。
不添加旧 04 的 invalid 必须为1分或难度必须 not_applicable 约束。

报告按交付行统计；跨集合重复分别计数，L2 按 (L1,L2) 组合计数。
切片允许重叠；空文件表示零条，不表示漏生成。可视化目录暂为空。
只有 internal/manifest.json 的 status=completed 且 delivery.status=completed 才表示整包完成。
"""


def package_documents(root: Path, manifest: dict[str, Any]) -> dict[Path, Any]:
    """Pure reconstruction used by both writing and independent validation."""
    package = external_root(root)
    report = json.loads((root / "reports/screening_run_report.json").read_text(encoding="utf-8"))
    summary = report["build_summary"]
    config = summary["config"]
    timestamp = manifest["created_at"].replace("-", "").replace(":", "").replace(".", "").replace("+", "_")
    stage1_times = [manifest.get("branches", {}).get(n, {}).get("stage1_elapsed_seconds") for n in LABELED_FILENAMES]
    sampling = {
        "meta": {"pipeline": "Screening Stage1 + downstream adapter", "version": "V4",
                 "run_id": manifest["run_id"], "p2_algorithm": manifest["contract"]["p2_algorithm"],
                 "config": config,
                 "sampling_elapsed_seconds": max(stage1_times) if all(t is not None for t in stage1_times) else None,
                 "sampling_time_scope": "max Stage1 branch wall time including admission/Challenge; legacy unavailable=null",
                 "stage1_reused": bool(manifest.get("stage1_source")),
                 "build_and_annotation_elapsed_seconds": manifest.get("stages", {}).get("p012_build", {}).get("timing", {}).get("active_duration_ms", 0) / 1000},
        "all_tags": {},
    }
    quality = {"run_id": manifest["run_id"], "quality_rubric_version": 1,
               "labeled_mapping_version": 11, "sets": {}}
    docs = {}
    all_rows = []
    for name in LABELED_FILENAMES:
        base = load_jsonl(package / base_filename(name))
        rows = load_jsonl(package / LABELED_FILENAMES[name])
        all_rows.extend(base)
        entry = {"total": len(base),
                 "final_by_sample_type": {k: sum(r["sample_type"] == k for r in base) for k in ("single_turn", "multi_turn")},
                 "L1_cov": len({r["L1"] for r in base}),
                 "L2_cov": len({(r["L1"], r["L2"]) for r in base})}
        if name == "P2":
            entry["adaptive"] = summary["p2_adaptive"]
        sampling[name] = entry
        quality["sets"][name] = {"total": len(rows), "distributions": {
            key: dict(sorted(Counter(str(v) for r in rows for v in
                 (r[key] if key == "quality_issue_types" else [r[key]])).items()))
            for key in QUALITY_EXPORT_FIELDS}}
        for filename, predicate in SLICES.items():
            path = package / "质量标注/subsets" / f"{name}-质量分层子集" / filename
            docs[path] = [row for row in rows if predicate(row)]
    tags = {"prob": ("prob_band", ("high", "mid", "boundary")),
            "freq": ("freq_band", ("hot", "warm", "cold")),
            "len": ("length_bin", ("short", "mid", "long")),
            "complexity": ("complexity", ("simple", "moderate", "complex")),
            "formality": ("formality", ("formal", "mixed", "colloquial"))}
    sampling["all_tags"] = {key: {v: sum(r[field] == v for r in all_rows) for v in values}
                            for key, (field, values) in tags.items()}
    docs[package / "sampling_report_v4.json"] = sampling
    docs[package / "质量标注/reports" / f"labeling_report_{timestamp}.json"] = quality
    return docs


def write_package(root: Path, manifest: dict[str, Any]) -> None:
    for path, value in package_documents(root, manifest).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        (write_jsonl if path.suffix == ".jsonl" else write_json)(path, value)
    (external_root(root) / "可视化").mkdir(parents=True, exist_ok=True)
    (root / "reports/字段标准与使用说明.md").write_text(FIELD_GUIDE, encoding="utf-8")


def validate_package(root: Path, manifest: dict[str, Any]) -> None:
    docs = package_documents(root, manifest)
    for path, expected in docs.items():
        actual = load_jsonl(path) if path.suffix == ".jsonl" else json.loads(path.read_text(encoding="utf-8"))
        if actual != expected:
            raise ValueError(f"交付报告或切片与数据不一致: {path.name}")
    expected_files = set(docs) | {external_root(root) / p for n in LABELED_FILENAMES
                                for p in (LABELED_FILENAMES[n], base_filename(n))}
    actual_files = {p for p in external_root(root).rglob("*") if p.is_file()}
    if actual_files != expected_files or not (external_root(root) / "可视化").is_dir():
        raise ValueError("交付文件清单不完整或包含残留文件")
    if (root / "reports/字段标准与使用说明.md").read_text(encoding="utf-8") != FIELD_GUIDE:
        raise ValueError("交付字段标准说明不一致")
