"""Normalize upstream records to the labeled schema of their target set."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .core import JsonObject, iter_jsonl


DIRECT_PASSTHROUGH_FIELDS = ("text", "cluster_prob", "frequency")
UPSTREAM_REQUIRED_TARGET_FIELDS = (
    "session_id",
    "target_query",
    "cluster_id",
    "L1",
    "L2",
    "set",
)
_MAPPED_FIELD_TYPES: dict[str, set[str]] = {
    "session_id": {"string"},
    "target_query": {"string"},
    "cluster_id": {"number", "string"},
    "L1": {"string"},
    "L2": {"string"},
    "set": {"string"},
}


class LabeledSchemaError(ValueError):
    """A sanitized labeled-schema contract failure."""

    def __init__(
        self,
        code: str,
        source_line: int,
        message: str,
        *,
        details: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.source_line = source_line
        self.details = details or {}


@dataclass(frozen=True)
class LabeledSchema:
    fields: tuple[str, ...]
    field_types: dict[str, str | None]
    set_value: str

    def audit_summary(self) -> JsonObject:
        return {
            "status": "valid",
            "fields": list(self.fields),
            "field_types": dict(self.field_types),
            "set_value": self.set_value,
        }


@dataclass(frozen=True)
class AlignmentResult:
    record: JsonObject
    null_fields: tuple[str, ...]
    overwritten_fields: tuple[str, ...]


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def inspect_labeled_schema(
    path: Path,
    *,
    required_fields: Iterable[str] = (),
) -> LabeledSchema:
    """Read and validate the stable record shape of one labeled input."""
    fields: tuple[str, ...] | None = None
    field_set: set[str] = set()
    observed_types: dict[str, set[str]] = {}
    set_value: str | None = None
    records = 0
    for source_line, row in iter_jsonl(path):
        records += 1
        current_fields = tuple(str(field) for field in row)
        current_set = set(current_fields)
        if fields is None:
            fields = current_fields
            field_set = current_set
            missing = sorted(set(required_fields) - field_set)
            if missing:
                raise LabeledSchemaError(
                    "labeled_schema_missing_required_fields",
                    source_line,
                    f"{path}:{source_line} labeled Schema 缺少必需字段: {missing}",
                    details={"missing_fields": missing},
                )
            observed_types = {field: set() for field in fields}
        elif current_set != field_set:
            missing = sorted(field_set - current_set)
            extra = sorted(current_set - field_set)
            raise LabeledSchemaError(
                "labeled_schema_field_mismatch",
                source_line,
                f"{path}:{source_line} labeled Schema 字段集合不一致",
                details={"missing_fields": missing, "extra_fields": extra},
            )

        for field in fields:
            value_type = _json_type(row[field])
            if value_type == "null":
                continue
            observed_types[field].add(value_type)
            if len(observed_types[field]) > 1:
                raise LabeledSchemaError(
                    "labeled_schema_type_mismatch",
                    source_line,
                    f"{path}:{source_line} labeled 字段 {field!r} 非空值类型不一致",
                    details={
                        "field": field,
                        "observed_types": sorted(observed_types[field]),
                    },
                )

        raw_set = row.get("set")
        if not isinstance(raw_set, str) or not raw_set.strip():
            raise LabeledSchemaError(
                "labeled_schema_invalid_set",
                source_line,
                f"{path}:{source_line} labeled 字段 set 必须是非空字符串",
                details={"field": "set"},
            )
        if set_value is None:
            set_value = raw_set
        elif raw_set != set_value:
            raise LabeledSchemaError(
                "labeled_schema_set_mismatch",
                source_line,
                f"{path}:{source_line} labeled 字段 set 存在多个值",
                details={"field": "set"},
            )

    if not records or fields is None or set_value is None:
        raise LabeledSchemaError(
            "labeled_schema_empty",
            0,
            f"{path} labeled 文件没有可用记录",
        )
    field_types = {
        field: next(iter(types)) if types else None
        for field, types in observed_types.items()
    }
    for field in required_fields:
        observed_type = field_types.get(field)
        allowed_types = _MAPPED_FIELD_TYPES.get(field)
        if (
            observed_type is not None
            and allowed_types is not None
            and observed_type not in allowed_types
        ):
            raise LabeledSchemaError(
                "labeled_schema_mapped_type_mismatch",
                1,
                f"{path}:1 labeled 映射字段 {field!r} 类型不兼容",
                details={
                    "field": field,
                    "observed_type": observed_type,
                    "allowed_types": sorted(allowed_types),
                },
            )
    return LabeledSchema(
        fields=fields,
        field_types=field_types,
        set_value=set_value,
    )


def l2_leaf_name(l1: str, l2: Any) -> str | None:
    """Convert the internal qualified L2 key to the labeled leaf name."""
    value = str(l2 or "").strip()
    if not value or value in {"未知", "__L1_ONLY__", f"{l1}/__L1_ONLY__"}:
        return None
    prefix = f"{l1}/"
    if not value.startswith(prefix) or not value[len(prefix):]:
        raise ValueError("upstream L2 与 L1 的限定路径不一致")
    return value[len(prefix):]


def align_upstream_record(
    source_record: JsonObject,
    schema: LabeledSchema,
    *,
    mapped_values: Mapping[str, Any],
    passthrough_fields: Iterable[str] = DIRECT_PASSTHROUGH_FIELDS,
) -> AlignmentResult:
    """Project one upstream record to a target labeled schema."""
    unknown_mappings = sorted(set(mapped_values) - set(schema.fields))
    if unknown_mappings:
        raise ValueError(
            f"目标 labeled Schema 缺少映射字段: {unknown_mappings}"
        )

    values: dict[str, Any] = {}
    for field in passthrough_fields:
        if field in schema.fields and field in source_record:
            values[field] = source_record[field]

    overwritten: list[str] = []
    for field, value in mapped_values.items():
        if field in source_record and source_record[field] != value:
            overwritten.append(field)
        values[field] = value

    aligned: JsonObject = {}
    null_fields: list[str] = []
    for field in schema.fields:
        value = values.get(field)
        expected_type = schema.field_types.get(field)
        actual_type = _json_type(value)
        if value is not None and expected_type is not None and actual_type != expected_type:
            raise ValueError(
                f"upstream 映射字段 {field!r} 类型 {actual_type} 与 labeled Schema {expected_type} 不一致"
            )
        aligned[field] = value
        if value is None:
            null_fields.append(field)
    return AlignmentResult(
        record=aligned,
        null_fields=tuple(null_fields),
        overwritten_fields=tuple(sorted(overwritten)),
    )
