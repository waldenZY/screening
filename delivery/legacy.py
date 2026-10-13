"""Deterministic screened-to-labeled v5 export."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..quality.two_stage import TASK_DIFFICULTIES
from ..shared.json_io import JsonObject, write_json, write_jsonl
from .screened import (
    SCREENED_CANONICAL_SCHEMA_VERSION,
    stable_record_sha256,
    validate_screened_canonical_record,
    validate_screened_canonical_records,
)


LEGACY_MAPPING_VERSION = 4
LEGACY_SCHEMA_NAME = "labeled_v5"
LEGACY_SET_VALUES = {
    "P0": "P0_smoke",
    "P1": "P1_benchmark",
    "P2": "P2_adversarial",
}
LEGACY_FILENAMES = {
    "P0": Path("P0-冒烟集") / "P0_smoke_set_v5_labeled.jsonl",
    "P1": Path("P1-基准集") / "P1_benchmark_set_v5_labeled.jsonl",
    "P2": Path("P2-对抗集") / "P2_adversarial_set_v5_labeled.jsonl",
}
LEGACY_FIELDS = (
    "text", "context_text", "target_query", "turn_count", "L1", "L2",
    "cluster_id", "cluster_prob", "frequency", "session_id", "set",
    "quality_task_difficulty", "intent_label", "executable",
)


class LegacyExportError(ValueError):
    """An exact labeled export mapping or validation failure."""


def legacy_fields(set_name: str) -> tuple[str, ...]:
    if set_name not in LEGACY_SET_VALUES:
        raise LegacyExportError(f"未知数据集: {set_name}")
    return LEGACY_FIELDS


def map_screened_to_legacy(record: Mapping[str, Any]) -> JsonObject:
    """Map one screened record without changing membership."""
    validate_screened_canonical_record(record)
    set_name = str(record["set"])
    candidate = record["candidate"]
    conversation = candidate["conversation"]
    context_text = " | ".join(conversation["context_turns"])
    values = {
        "text": " | ".join([*conversation["context_turns"], conversation["target_query"]]),
        "context_text": context_text,
        "target_query": conversation["target_query"],
        "turn_count": conversation["turn_count"],
        "L1": candidate["taxonomy"]["l1"],
        "L2": candidate["taxonomy"]["l2"],
        "cluster_id": candidate["taxonomy"]["cluster_id"],
        "cluster_prob": candidate["signals"]["cluster_probability"],
        "frequency": candidate["signals"]["frequency"],
        "session_id": candidate["source"]["session_id"],
        "set": LEGACY_SET_VALUES[set_name],
        "quality_task_difficulty": record["quality"]["task_difficulty"],
        "intent_label": record["delivery_labels"]["intent_label"],
        "executable": record["delivery_labels"]["executable"],
    }
    mapped = {field: values[field] for field in legacy_fields(set_name)}
    validate_legacy_record(set_name, mapped, canonical_record=record)
    return mapped


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def validate_legacy_record(
    set_name: str,
    record: Mapping[str, Any],
    *,
    canonical_record: Mapping[str, Any] | None = None,
) -> None:
    """Independently check the 14-field order, types, and source mapping."""
    if tuple(record) != legacy_fields(set_name):
        raise LegacyExportError("legacy 字段名称或顺序不符合当前 labeled_v5")
    if any(value is None for value in record.values()):
        raise LegacyExportError("legacy 必需字段不得为 null")
    for field in (
        "text", "context_text", "target_query", "L1", "L2", "session_id",
        "set", "quality_task_difficulty", "intent_label",
    ):
        value = record[field]
        if not isinstance(value, str) or (field != "context_text" and not value):
            raise LegacyExportError(f"legacy {field} 必须是非空字符串")
    for field in ("turn_count", "cluster_id", "frequency"):
        if type(record[field]) is not int:
            raise LegacyExportError(f"legacy {field} 必须是整数")
    if record["turn_count"] < 1 or record["frequency"] < 0:
        raise LegacyExportError("legacy 轮次或频次非法")
    if not _is_number(record["cluster_prob"]) or not 0 <= record["cluster_prob"] <= 1:
        raise LegacyExportError("legacy cluster_prob 非法")
    if type(record["executable"]) is not int or record["executable"] not in (0, 1):
        raise LegacyExportError("legacy executable 必须是 0/1 整数")
    if record["intent_label"] not in {"task", "qa"}:
        raise LegacyExportError("legacy intent_label 非法")
    if record["quality_task_difficulty"] not in TASK_DIFFICULTIES:
        raise LegacyExportError("legacy task difficulty 枚举非法")
    if record["set"] != LEGACY_SET_VALUES[set_name]:
        raise LegacyExportError("legacy set 值非法")
    expected_text = " | ".join(
        [record["context_text"], record["target_query"]]
        if record["context_text"] else [record["target_query"]]
    )
    if record["text"] != expected_text:
        raise LegacyExportError("legacy 会话字段彼此不一致")
    expected_turns = 1 if not record["context_text"] else record["context_text"].count(" | ") + 2
    if record["turn_count"] != expected_turns:
        raise LegacyExportError("legacy turn_count 与会话字段不一致")
    if canonical_record is not None:
        expected = map_screened_to_legacy_unchecked(canonical_record)
        if dict(record) != expected:
            raise LegacyExportError("legacy 与 canonical 映射值不一致")


def map_screened_to_legacy_unchecked(record: Mapping[str, Any]) -> JsonObject:
    """Construct expected export independently for publication checks."""
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
        "set": LEGACY_SET_VALUES[set_name],
        "quality_task_difficulty": record["quality"]["task_difficulty"],
        "intent_label": record["quality"]["intent_label"],
        "executable": record["delivery_labels"]["executable"],
    }
    return {field: direct[field] for field in legacy_fields(set_name)}


def export_legacy_branch(
    set_name: str,
    records: Sequence[JsonObject],
    branch_root: Path,
) -> JsonObject:
    """Write internal records, labeled rows, and per-row provenance."""
    validate_screened_canonical_records(set_name, records)
    legacy_records = [map_screened_to_legacy(record) for record in records]
    canonical_path = branch_root / "screened.canonical.jsonl"
    legacy_path = branch_root / LEGACY_FILENAMES[set_name].name
    provenance_path = branch_root / "export_provenance.jsonl"
    provenance = []
    for index, (canonical, legacy) in enumerate(zip(records, legacy_records), start=1):
        candidate = canonical["candidate"]
        selection = canonical["selection"]
        provenance.append({
            "output_index": index,
            "source_identity_sha256": candidate["source_identity_sha256"],
            "source_record_sha256": candidate["source"]["record_sha256"],
            "canonical_record_sha256": stable_record_sha256(canonical),
            "legacy_record_sha256": stable_record_sha256(legacy),
            "origin": selection["origin"],
            "round_id": selection["round_id"],
            "candidate_task_id": selection["candidate_task_id"],
            "stage1_task_id": selection["stage1_task_id"],
            "stage2_task_id": selection["stage2_task_id"],
            "legacy_mapping_version": LEGACY_MAPPING_VERSION,
        })
    write_jsonl(canonical_path, records)
    write_jsonl(legacy_path, legacy_records)
    write_jsonl(provenance_path, provenance)
    report: JsonObject = {
        "screened_schema_version": SCREENED_CANONICAL_SCHEMA_VERSION,
        "legacy_schema": LEGACY_SCHEMA_NAME,
        "legacy_mapping_version": LEGACY_MAPPING_VERSION,
        "set": set_name,
        "records": len(records),
        "field_count": len(legacy_fields(set_name)),
        "fields": list(legacy_fields(set_name)),
        "null_values": 0,
        "lossy_enum_mappings": 0,
        "rounded_numeric_fields": [],
        "files": {
            "canonical": canonical_path.name,
            "legacy": legacy_path.name,
            "provenance": provenance_path.name,
        },
    }
    write_json(branch_root / "legacy_export_report.json", report)
    return report
