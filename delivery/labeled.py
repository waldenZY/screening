"""Deterministic screened-to-labeled v6 export."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..quality.runner import TASK_DIFFICULTIES
from ..shared.json_io import JsonObject, write_json, write_jsonl
from .screened import (
    SCREENED_CANONICAL_SCHEMA_VERSION,
    stable_record_sha256,
    validate_screened_canonical_record,
    validate_screened_canonical_records,
)


LABELED_MAPPING_VERSION = 7
LABELED_SCHEMA_NAME = "labeled_v6"
LABELED_SET_VALUES = {
    "P0": "P0_smoke",
    "P1": "P1_benchmark",
    "P2": "P2_adversarial",
}
LABELED_FILENAMES = {
    "P0": Path("P0-冒烟集") / "P0_smoke_set_v6_labeled.jsonl",
    "P1": Path("P1-基准集") / "P1_benchmark_set_v6_labeled.jsonl",
    "P2": Path("P2-对抗集") / "P2_adversarial_set_v6_labeled.jsonl",
}
LABELED_FIELDS = (
    "text", "context_text", "target_query", "turn_count", "L1", "L2",
    "cluster_id", "cluster_prob", "frequency", "session_id", "set",
    "quality_task_difficulty", "intent_label", "executable", "stage1",
)


class LabeledExportError(ValueError):
    """An exact labeled export mapping or validation failure."""


def labeled_fields(set_name: str) -> tuple[str, ...]:
    if set_name not in LABELED_SET_VALUES:
        raise LabeledExportError(f"未知数据集: {set_name}")
    return LABELED_FIELDS


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
    """Independently check the 15-field order, types, and source mapping."""
    from ..quality.stage1 import validate_stage1_result
    validate_stage1_result({"stage1": record.get("stage1")})
    if tuple(record) != labeled_fields(set_name):
        raise LabeledExportError("labeled 字段名称或顺序不符合当前 labeled_v6")
    if any(value is None for value in record.values()):
        raise LabeledExportError("labeled 必需字段不得为 null")
    for field in (
        "text", "context_text", "target_query", "L1", "L2", "session_id",
        "set", "quality_task_difficulty", "intent_label",
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
    if record["quality_task_difficulty"] not in TASK_DIFFICULTIES:
        raise LabeledExportError("labeled task difficulty 枚举非法")
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
    turns = (record["context_text"].split(" | ") if record["context_text"] else []) + [record["target_query"]]
    stage1 = validate_stage1_result({"stage1": record["stage1"]}, turns)["stage1"]
    if (not stage1["screening"]["admitted"] or not stage1["policy"]["require_category_exact"]
            or stage1["category"] != {"status": "reviewed", "fit": "exact"}):
        raise LabeledExportError("交付包含未准入或未核验类别的样本")
    if record["executable"] and (stage1["screening"]["route"] != "direct"
            or record["turn_count"] != 1 or len(record["target_query"]) <= 2):
        raise LabeledExportError("executable 不符合单轮直接测试条件")
    if canonical_record is not None:
        expected = _labeled_values(canonical_record)
        if dict(record) != expected:
            raise LabeledExportError("labeled 与 canonical 映射值不一致")


def _labeled_values(record: Mapping[str, Any]) -> JsonObject:
    """Project canonical fields for export and recorded-output validation."""
    candidate = record["candidate"]
    conversation = candidate["conversation"]
    set_name = str(record["set"])
    direct = {
        "text": " | ".join([*conversation["context_turns"], conversation["target_query"]]),
        "context_text": " | ".join(conversation["context_turns"]),
        "target_query": conversation["target_query"],
        "turn_count": len(conversation["context_turns"]) + 1,
        "L1": candidate["taxonomy"]["l1"],
        "L2": candidate["taxonomy"]["l2"],
        "cluster_id": candidate["taxonomy"]["cluster_id"],
        "cluster_prob": candidate["signals"]["cluster_probability"],
        "frequency": candidate["signals"]["frequency"],
        "session_id": candidate["source"]["session_id"],
        "set": LABELED_SET_VALUES[set_name],
        "quality_task_difficulty": record["quality"]["task_difficulty"],
        "intent_label": record["quality"]["intent_label"],
        "executable": record["delivery_labels"]["executable"],
        "stage1": record["quality"]["stage1"],
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
            "stage1_task_id": selection["stage1_task_id"],
            "stage2_task_id": selection["stage2_task_id"],
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
        "rounded_numeric_fields": [],
        "files": {
            "canonical": canonical_path.name,
            "labeled": labeled_path.name,
            "provenance": provenance_path.name,
        },
    }
    write_json(branch_root / "labeled_export_report.json", report)
    return report
