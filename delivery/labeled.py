"""Stage2 deterministic downstream_labeled_v1 export (fixed v4 filenames)."""

from __future__ import annotations

import math
import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..shared.json_io import JsonObject, write_json, write_jsonl
from .screened import (
    SCREENED_CANONICAL_SCHEMA_VERSION,
    stable_record_sha256,
    validate_screened_canonical_record,
    validate_screened_canonical_records,
)


LABELED_MAPPING_VERSION = 10
LABELED_SCHEMA_NAME = "downstream_labeled_v1"
LABELED_SET_VALUES = {
    "P0": "P0_smoke",
    "P1": "P1_benchmark",
    "P2": "P2_adversarial",
}
LABELED_FILENAMES = {
    "P0": Path("P0-冒烟集") / "P0_smoke_set_v4_labeled.jsonl",
    "P1": Path("P1-基准集") / "P1_benchmark_set_v4_labeled.jsonl",
    "P2": Path("P2-对抗集") / "P2_adversarial_set_v4_labeled.jsonl",
}
LABELED_FIELDS = (
    "text", "raw_text", "context_text", "target_query", "turn_count", "sample_type",
    "L1", "L2", "cluster_id", "cluster_prob", "prob_band", "frequency", "freq_band",
    "norm_freq", "relevance", "length_bin", "complexity", "formality", "session_id", "set",
    "quality_category_fit", "quality_validity", "quality_intent_clarity",
    "quality_issue_types", "quality_overall_score", "intent_label", "executable", "stage1", "provenance",
)


class LabeledExportError(ValueError):
    """An exact labeled export mapping or validation failure."""


def labeled_fields(set_name: str) -> tuple[str, ...]:
    if set_name not in LABELED_SET_VALUES:
        raise LabeledExportError(f"未知数据集: {set_name}")
    relevance = "relevance_p2" if set_name == "P2" else "relevance_p0"
    return tuple(relevance if field == "relevance" else field for field in LABELED_FIELDS)


def map_screened_to_labeled(record: Mapping[str, Any]) -> JsonObject:
    """Map one screened record without changing membership."""
    validate_screened_canonical_record(record)
    set_name = str(record["set"])
    mapped = _labeled_values(record)
    validate_labeled_record(set_name, mapped, canonical_record=record)
    return mapped


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def validate_labeled_record(
    set_name: str,
    record: Mapping[str, Any],
    *,
    canonical_record: Mapping[str, Any] | None = None,
) -> None:
    """Independently check all 29 fields, types, enums and frozen source mapping."""
    from ..quality.admission import validate_admission_result, mapped_intent_label
    if not isinstance(record.get("stage1"), Mapping) or set(record["stage1"]) != {"admission", "selection", "challenge"}:
        raise LabeledExportError("stage1 透传必须包含 admission、selection、challenge")
    validate_admission_result({"admission": record["stage1"]["admission"]})
    if tuple(record) != labeled_fields(set_name):
        raise LabeledExportError("labeled 字段名称或顺序不符合 downstream_labeled_v1")
    if any(value is None for value in record.values()):
        raise LabeledExportError("labeled 必需字段不得为 null")
    for field in (
        "text", "raw_text", "context_text", "target_query", "L1", "L2", "session_id",
        "set", "intent_label",
    ):
        value = record[field]
        if not isinstance(value, str) or (field != "context_text" and not value):
            raise LabeledExportError(f"labeled {field} 必须是非空字符串")
    for field in ("turn_count", "cluster_id", "frequency"):
        if type(record[field]) is not int:
            raise LabeledExportError(f"labeled {field} 必须是整数")
    if record["turn_count"] < 1 or record["frequency"] < 0:
        raise LabeledExportError("labeled 轮次或频次非法")
    if not _is_number(record["cluster_prob"]) or not 0 <= record["cluster_prob"] <= 1:
        raise LabeledExportError("labeled cluster_prob 非法")
    if type(record["executable"]) is not int or record["executable"] not in (0, 1):
        raise LabeledExportError("labeled executable 必须是 0/1 整数")
    if record["intent_label"] not in {"task", "qa"}:
        raise LabeledExportError("labeled intent_label 非法")
    if record["set"] != LABELED_SET_VALUES[set_name]:
        raise LabeledExportError("labeled set 值非法")
    expected_text = " | ".join(
        [record["context_text"], record["target_query"]]
        if record["context_text"] else [record["target_query"]]
    )
    if record["text"] != expected_text:
        raise LabeledExportError("labeled 会话字段彼此不一致")
    expected_turns = 1 if not record["context_text"] else record["context_text"].count(" | ") + 2
    if record["turn_count"] != expected_turns:
        raise LabeledExportError("labeled turn_count 与会话字段不一致")
    from ..quality.annotation import QUALITY_FIELDS, validate_quality_annotation
    from ..candidate.contract import PROBABILITY_BANDS, FREQUENCY_BANDS, LENGTH_BANDS, COMPLEXITY_LEVELS
    validate_quality_annotation({key: record[key] for key in QUALITY_FIELDS})
    for field, choices in (("prob_band", PROBABILITY_BANDS), ("freq_band", FREQUENCY_BANDS),
                           ("length_bin", LENGTH_BANDS), ("complexity", COMPLEXITY_LEVELS),
                           ("formality", {"formal", "mixed", "colloquial"})):
        if record[field] not in choices:
            raise LabeledExportError(f"{field} 枚举非法")
    relevance = "relevance_p2" if set_name == "P2" else "relevance_p0"
    for field in ("norm_freq", relevance):
        if not _is_number(record[field]) or not 0 <= record[field] <= 1 or record[field] != round(record[field], 4):
            raise LabeledExportError(f"{field} 必须是四位小数精度的0～1数值")
    if record["sample_type"] != ("multi_turn" if record["turn_count"] > 1 else "single_turn"):
        raise LabeledExportError("sample_type 与轮数不一致")
    admission = validate_admission_result({"admission": record["stage1"]["admission"]})["admission"]
    if record["quality_category_fit"] != admission["category"]["fit"]:
        raise LabeledExportError("quality_category_fit 必须来自类别核验")
    if not isinstance(record["provenance"], Mapping) or not record["provenance"].get("run_id"):
        raise LabeledExportError("provenance 缺少运行来源")
    if record["intent_label"] != mapped_intent_label(admission["features"]):
        raise LabeledExportError("labeled intent_label 必须映射自 准入评估")
    if (not admission["screening"]["admitted"] or not admission["policy"]["require_category_exact"]
            or admission["category"] != {"status": "reviewed", "fit": "exact"}):
        raise LabeledExportError("交付包含未准入或未核验类别的样本")
    if record["executable"] and (admission["screening"]["route"] != "direct"
            or record["turn_count"] != 1 or len(record["target_query"]) <= 2):
        raise LabeledExportError("executable 不符合单轮直接测试条件")
    if canonical_record is not None:
        expected = _labeled_values(canonical_record)
        if dict(record) != expected:
            raise LabeledExportError("labeled 与 canonical 映射值不一致")


def _labeled_values(record: Mapping[str, Any]) -> JsonObject:
    """Project canonical fields for export and recorded-output validation."""
    candidate = record["candidate"]
    from ..quality.annotation import validate_quality_annotation
    if "downstream_quality" not in record:
        raise LabeledExportError("Stage2 下游质量标注未完成，不得导出")
    annotation = validate_quality_annotation(record["downstream_quality"])
    conversation = candidate["conversation"]
    set_name = str(record["set"])
    direct = {
        "text": " | ".join([*conversation["context_turns"], conversation["target_query"]]),
        "raw_text": candidate["source"]["raw_text"],
        "context_text": " | ".join(conversation["context_turns"]),
        "target_query": conversation["target_query"],
        "turn_count": len(conversation["context_turns"]) + 1,
        "sample_type": conversation["sample_type"],
        "L1": candidate["taxonomy"]["l1"],
        "L2": candidate["taxonomy"]["l2"],
        "cluster_id": candidate["taxonomy"]["cluster_id"],
        "cluster_prob": candidate["signals"]["cluster_probability"],
        "prob_band": candidate["features"]["probability_band"],
        "frequency": candidate["signals"]["frequency"],
        "freq_band": candidate["features"]["frequency_band"],
        "norm_freq": round(candidate["features"]["normalized_frequency"], 4),
        ("relevance_p2" if set_name == "P2" else "relevance_p0"): round(record["sampling_scores"]["p2" if set_name == "P2" else "p0"], 4),
        "length_bin": candidate["features"]["length_band"],
        "complexity": candidate["features"]["complexity"],
        "formality": candidate["features"]["formality"],
        "session_id": candidate["source"]["session_id"],
        "set": LABELED_SET_VALUES[set_name],
        "intent_label": record["quality"]["intent_label"],
        "executable": record["delivery_labels"]["executable"],
        "quality_category_fit": record["quality"]["admission"]["category"]["fit"],
        **annotation,
        "stage1": copy.deepcopy({"admission": record["quality"]["admission"], "selection": record["selection"], "challenge": record["challenge"]}),
        "provenance": copy.deepcopy(record["provenance"]),
    }
    return {field: direct[field] for field in labeled_fields(set_name)}


def export_labeled_branch(
    set_name: str,
    records: Sequence[JsonObject],
    branch_root: Path,
) -> JsonObject:
    """Write internal records, labeled rows, and per-row provenance."""
    validate_screened_canonical_records(set_name, records)
    labeled_records = [map_screened_to_labeled(record) for record in records]
    canonical_path = branch_root / "screened.canonical.jsonl"
    labeled_path = branch_root / LABELED_FILENAMES[set_name].name
    provenance_path = branch_root / "export_provenance.jsonl"
    provenance = []
    for index, (canonical, labeled) in enumerate(zip(records, labeled_records), start=1):
        candidate = canonical["candidate"]
        selection = canonical["selection"]
        provenance.append({
            "output_index": index,
            "source_identity_sha256": candidate["source_identity_sha256"],
            "source_record_sha256": candidate["source"]["record_sha256"],
            "canonical_record_sha256": stable_record_sha256(canonical),
            "labeled_record_sha256": stable_record_sha256(labeled),
            "origin": selection["origin"],
            "round_id": selection["round_id"],
            "candidate_task_id": selection["candidate_task_id"],
            "admission_task_id": selection["admission_task_id"],
            "labeled_mapping_version": LABELED_MAPPING_VERSION,
        })
    write_jsonl(canonical_path, records)
    write_jsonl(labeled_path, labeled_records)
    write_jsonl(provenance_path, provenance)
    report: JsonObject = {
        "screened_schema_version": SCREENED_CANONICAL_SCHEMA_VERSION,
        "labeled_schema": LABELED_SCHEMA_NAME,
        "labeled_mapping_version": LABELED_MAPPING_VERSION,
        "set": set_name,
        "records": len(records),
        "field_count": len(labeled_fields(set_name)),
        "fields": list(labeled_fields(set_name)),
        "null_values": 0,
        "lossy_enum_mappings": 0,
        "rounded_numeric_fields": ["norm_freq", "relevance_p2" if set_name == "P2" else "relevance_p0"],
        "files": {
            "canonical": canonical_path.name,
            "labeled": labeled_path.name,
            "provenance": provenance_path.name,
        },
    }
    write_json(branch_root / "labeled_export_report.json", report)
    return report
