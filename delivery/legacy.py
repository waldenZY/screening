"""Deterministic canonical-to-labeled compatibility export."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..candidate.contract import (
    COMPLEXITY_LEVELS,
    FORMALITY_LEVELS,
    FREQUENCY_BANDS,
    LENGTH_BANDS,
    PROBABILITY_BANDS,
    SAMPLE_TYPES,
)
from ..shared.json_io import JsonObject, write_json, write_jsonl
from .screened import (
    SCREENED_CANONICAL_SCHEMA_VERSION,
    stable_record_sha256,
    validate_screened_canonical_record,
    validate_screened_canonical_records,
)
from ..quality.two_stage import (
    CLASSIFICATION_DIFFICULTIES,
    ISSUE_TYPES,
    STAGE1_ENUMS,
    TASK_DIFFICULTIES,
)


LEGACY_MAPPING_VERSION = 2
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
LEGACY_COMMON_FIELDS = (
    "text", "raw_text", "context_text", "target_query", "turn_count",
    "sample_type", "L1", "L2", "cluster_id", "cluster_prob", "prob_band",
    "frequency", "freq_band", "norm_freq",
)
LEGACY_TRAILING_FIELDS = (
    "length_bin", "complexity", "formality", "session_id", "set",
    "quality_category_fit", "quality_semantic_completeness",
    "quality_intent_clarity", "quality_validity",
    "quality_classification_difficulty", "quality_task_difficulty",
    "quality_issue_types", "quality_overall_score",
    "intent_label", "executable",
)


class LegacyExportError(ValueError):
    """An exact legacy mapping or validation failure."""


def legacy_fields(set_name: str) -> tuple[str, ...]:
    if set_name not in LEGACY_SET_VALUES:
        raise LegacyExportError(f"未知数据集: {set_name}")
    relevance = "relevance_p2" if set_name == "P2" else "relevance_p0"
    return LEGACY_COMMON_FIELDS + (relevance,) + LEGACY_TRAILING_FIELDS


def map_screened_to_legacy(record: Mapping[str, Any]) -> JsonObject:
    """Map one screened record without selecting, filtering, or filling nulls."""
    validate_screened_canonical_record(record)
    set_name = str(record["set"])
    candidate = record["candidate"]
    conversation = candidate["conversation"]
    taxonomy = candidate["taxonomy"]
    signals = candidate["signals"]
    features = candidate["features"]
    quality = record["quality"]
    relevance_name = "relevance_p2" if set_name == "P2" else "relevance_p0"
    relevance_key = "p2" if set_name == "P2" else "p0"
    context_text = " | ".join(conversation["context_turns"])
    legacy_text = (
        conversation["target_query"]
        if not context_text
        else f"{context_text} | {conversation['target_query']}"
    )
    values: dict[str, Any] = {
        "text": legacy_text,
        "raw_text": candidate["source"]["raw_text"],
        "context_text": context_text,
        "target_query": conversation["target_query"],
        "turn_count": conversation["turn_count"],
        "sample_type": conversation["sample_type"],
        "L1": taxonomy["l1"],
        "L2": taxonomy["l2"],
        "cluster_id": taxonomy["cluster_id"],
        "cluster_prob": signals["cluster_probability"],
        "prob_band": features["probability_band"],
        "frequency": signals["frequency"],
        "freq_band": features["frequency_band"],
        "norm_freq": round(features["normalized_frequency"], 4),
        relevance_name: round(features["relevance"][relevance_key], 4),
        "length_bin": features["length_band"],
        "complexity": features["complexity"],
        "formality": features["formality"],
        "session_id": candidate["source"]["session_id"],
        "set": LEGACY_SET_VALUES[set_name],
        "quality_category_fit": quality["category_fit"],
        "quality_semantic_completeness": quality["semantic_completeness"],
        "quality_intent_clarity": quality["intent_clarity"],
        "quality_validity": quality["validity"],
        "quality_classification_difficulty": quality["classification_difficulty"],
        "quality_task_difficulty": quality["task_difficulty"],
        "quality_issue_types": list(quality["issue_types"]),
        "quality_overall_score": quality["overall_score"],
        "intent_label": record["delivery_labels"]["intent_label"],
        "executable": record["delivery_labels"]["executable"],
    }
    mapped = OrderedDict((field, values[field]) for field in legacy_fields(set_name))
    validate_legacy_record(set_name, mapped, canonical_record=record)
    return dict(mapped)


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
    """Independently assert exact old field order, types, enums, and values."""
    expected_fields = legacy_fields(set_name)
    if tuple(record) != expected_fields:
        raise LegacyExportError("legacy 字段名称或顺序不符合 labeled_v5")
    if any(value is None for value in record.values()):
        raise LegacyExportError("legacy 必需字段不得为 null")
    string_fields = {
        "text", "raw_text", "context_text", "target_query", "sample_type", "L1", "L2",
        "prob_band", "freq_band", "length_bin", "complexity", "formality", "session_id",
        "set", "quality_category_fit", "quality_semantic_completeness",
        "quality_intent_clarity", "quality_validity", "quality_classification_difficulty",
        "quality_task_difficulty",
        "intent_label",
    }
    for field in string_fields:
        value = record[field]
        if not isinstance(value, str) or (field != "context_text" and not value):
            raise LegacyExportError(f"legacy {field} 必须是非空字符串")
    for field in ("turn_count", "cluster_id", "frequency", "quality_overall_score"):
        if isinstance(record[field], bool) or not isinstance(record[field], int):
            raise LegacyExportError(f"legacy {field} 必须是整数")
    if type(record["executable"]) is not int or record["executable"] not in (0, 1):
        raise LegacyExportError("legacy executable 必须是 0/1 整数")
    if record["intent_label"] not in {"task", "qa"}:
        raise LegacyExportError("legacy intent_label 非法")
    relevance_field = "relevance_p2" if set_name == "P2" else "relevance_p0"
    for field in ("cluster_prob", "norm_freq", relevance_field):
        if not _is_number(record[field]):
            raise LegacyExportError(f"legacy {field} 必须是有限数值")
    if record["sample_type"] not in SAMPLE_TYPES:
        raise LegacyExportError("legacy sample_type 枚举非法")
    if record["prob_band"] not in PROBABILITY_BANDS:
        raise LegacyExportError("legacy prob_band 枚举非法")
    if record["freq_band"] not in FREQUENCY_BANDS:
        raise LegacyExportError("legacy freq_band 枚举非法")
    if record["length_bin"] not in LENGTH_BANDS:
        raise LegacyExportError("legacy length_bin 枚举非法")
    if record["complexity"] not in COMPLEXITY_LEVELS:
        raise LegacyExportError("legacy complexity 枚举非法")
    if record["formality"] not in FORMALITY_LEVELS:
        raise LegacyExportError("legacy formality 枚举非法")
    for field, enum_key in (
        ("quality_category_fit", "category_fit"),
        ("quality_semantic_completeness", "semantic_completeness"),
        ("quality_intent_clarity", "intent_clarity"),
        ("quality_validity", "validity"),
    ):
        if record[field] not in STAGE1_ENUMS[enum_key]:
            raise LegacyExportError(f"legacy {field} 枚举非法")
    if record["quality_classification_difficulty"] not in CLASSIFICATION_DIFFICULTIES:
        raise LegacyExportError("legacy classification difficulty 枚举非法")
    if record["quality_task_difficulty"] not in TASK_DIFFICULTIES:
        raise LegacyExportError("legacy task difficulty 枚举非法")
    issues = record["quality_issue_types"]
    if (
        not isinstance(issues, list) or not issues
        or any(not isinstance(item, str) or item not in ISSUE_TYPES for item in issues)
    ):
        raise LegacyExportError("legacy quality_issue_types 非法")
    if record["set"] != LEGACY_SET_VALUES[set_name]:
        raise LegacyExportError("legacy set 值非法")
    expected_context = "" if record["sample_type"] == "single_turn" else record["context_text"]
    expected_text = (
        record["target_query"]
        if not expected_context
        else f"{expected_context} | {record['target_query']}"
    )
    if record["context_text"] != expected_context or record["text"] != expected_text:
        raise LegacyExportError("legacy 会话字段彼此不一致")
    if record["turn_count"] != (1 if not expected_context else expected_context.count(" | ") + 2):
        raise LegacyExportError("legacy turn_count 与会话字段不一致")
    if canonical_record is not None:
        expected = map_screened_to_legacy_unchecked(canonical_record)
        if dict(record) != expected:
            raise LegacyExportError("legacy 与 canonical 映射值不一致")


def map_screened_to_legacy_unchecked(record: Mapping[str, Any]) -> JsonObject:
    """Independent expected-value construction used only by validation."""
    candidate = record["candidate"]
    conversation = candidate["conversation"]
    set_name = str(record["set"])
    relevance_field = "relevance_p2" if set_name == "P2" else "relevance_p0"
    relevance_key = "p2" if set_name == "P2" else "p0"
    direct = {
        "text": " | ".join([*conversation["context_turns"], conversation["target_query"]]),
        "raw_text": candidate["source"]["raw_text"],
        "context_text": " | ".join(conversation["context_turns"]),
        "target_query": conversation["target_query"],
        "turn_count": len(conversation["context_turns"]) + 1,
        "sample_type": "multi_turn" if conversation["context_turns"] else "single_turn",
        "L1": candidate["taxonomy"]["l1"],
        "L2": candidate["taxonomy"]["l2"],
        "cluster_id": candidate["taxonomy"]["cluster_id"],
        "cluster_prob": candidate["signals"]["cluster_probability"],
        "prob_band": candidate["features"]["probability_band"],
        "frequency": candidate["signals"]["frequency"],
        "freq_band": candidate["features"]["frequency_band"],
        "norm_freq": round(candidate["features"]["normalized_frequency"], 4),
        relevance_field: round(candidate["features"]["relevance"][relevance_key], 4),
        "length_bin": candidate["features"]["length_band"],
        "complexity": candidate["features"]["complexity"],
        "formality": candidate["features"]["formality"],
        "session_id": candidate["source"]["session_id"],
        "set": LEGACY_SET_VALUES[set_name],
        **{f"quality_{key}": value for key, value in record["quality"].items()},
        "intent_label": record["delivery_labels"]["intent_label"],
        "executable": record["delivery_labels"]["executable"],
    }
    return {field: direct[field] for field in legacy_fields(set_name)}


def export_legacy_branch(
    set_name: str,
    records: Sequence[JsonObject],
    branch_root: Path,
) -> JsonObject:
    """Write authoritative canonical, legacy labeled, and per-row provenance."""
    validate_screened_canonical_records(set_name, records)
    legacy_records = [map_screened_to_legacy(record) for record in records]
    canonical_path = branch_root / "screened.canonical.jsonl"
    legacy_path = branch_root / LEGACY_FILENAMES[set_name].name
    provenance_path = branch_root / "export_provenance.jsonl"
    provenance = []
    for index, (canonical, legacy) in enumerate(zip(records, legacy_records), start=1):
        candidate = canonical["candidate"]
        selection = canonical["selection"]
        provenance.append(
            {
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
            }
        )
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
        "rounded_numeric_fields": ["norm_freq", "relevance_p2" if set_name == "P2" else "relevance_p0"],
        "files": {
            "canonical": canonical_path.name,
            "legacy": legacy_path.name,
            "provenance": provenance_path.name,
        },
    }
    write_json(branch_root / "legacy_export_report.json", report)
    return report
