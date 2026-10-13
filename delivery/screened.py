"""Authoritative screened-record contract for the current pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..candidate.contract import validate_canonical_candidate
from ..shared.json_io import JsonObject
from ..quality.two_stage import (
    CHALLENGE_FIELDS,
    STAGE1_FIELDS,
    STAGE2_FIELDS,
    stage1_passes,
    validate_challenge_result,
    validate_stage1_result,
    validate_stage2_result,
)


SCREENED_CANONICAL_SCHEMA_VERSION = 6
SET_NAMES = frozenset({"P0", "P1", "P2"})
SELECTION_ORIGINS = frozenset({"initial_wave", "supplement_wave"})
_HASH = re.compile(r"[0-9a-f]{64}")


class ScreenedCanonicalError(ValueError):
    """A non-sensitive screened-record contract violation."""


def stable_record_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_screened_canonical_record(
    set_name: str,
    candidate: JsonObject,
    stage1_result: Mapping[str, Any],
    stage2_result: Mapping[str, Any],
    delivery_labels: Mapping[str, Any],
    *,
    challenge_result: Mapping[str, Any] | None = None,
    challenge_task_id: str | None = None,
    origin: str,
    round_id: int,
    candidate_task_id: str,
    stage1_task_id: str,
    stage2_task_id: str,
) -> JsonObject:
    """Build and independently validate one authoritative screened record."""
    validate_canonical_candidate(candidate)
    screened_candidate = {
        **candidate,
        "features": {
            "length_bin": candidate["features"]["length_band"],
            "complexity": candidate["features"]["complexity"],
        },
    }
    record: JsonObject = {
        "screened_schema_version": SCREENED_CANONICAL_SCHEMA_VERSION,
        "set": set_name,
        "candidate": screened_candidate,
        "quality": {
            **validate_stage1_result(dict(stage1_result)),
            **validate_stage2_result(dict(stage2_result), stage1_result),
        },
        "delivery_labels": dict(delivery_labels),
        "challenge": (
            None
            if set_name != "P2"
            else {
                **validate_challenge_result(dict(challenge_result or {})),
                "challenge_task_id": challenge_task_id,
            }
        ),
        "selection": {
            "origin": origin,
            "round_id": round_id,
            "candidate_task_id": candidate_task_id,
            "stage1_task_id": stage1_task_id,
            "stage2_task_id": stage2_task_id,
        },
    }
    validate_screened_canonical_record(record)
    return record


def validate_screened_canonical_record(record: Mapping[str, Any]) -> None:
    """Validate exact shape, admission, labels, and selection provenance."""
    if not isinstance(record, Mapping):
        raise ScreenedCanonicalError("screened canonical 必须是对象")
    expected = {
        "screened_schema_version", "set", "candidate", "quality",
        "challenge", "selection", "delivery_labels"
    }
    if set(record) != expected:
        raise ScreenedCanonicalError("screened canonical 顶层字段不一致")
    if record.get("screened_schema_version") != SCREENED_CANONICAL_SCHEMA_VERSION:
        raise ScreenedCanonicalError("screened canonical schema_version 非法")
    set_name = record.get("set")
    if set_name not in SET_NAMES:
        raise ScreenedCanonicalError("screened canonical set 非法")

    candidate = record.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ScreenedCanonicalError("screened canonical candidate 必须是对象")
    validate_canonical_candidate(candidate, screened_features=True)

    challenge = record.get("challenge")
    if set_name == "P2":
        expected_challenge_fields = CHALLENGE_FIELDS | {"challenge_task_id"}
        if not isinstance(challenge, Mapping) or set(challenge) != expected_challenge_fields:
            raise ScreenedCanonicalError("P2 challenge 字段不一致")
        validated_challenge = validate_challenge_result(
            {key: challenge[key] for key in CHALLENGE_FIELDS}
        )
        if not validated_challenge["is_challenge"]:
            raise ScreenedCanonicalError("P2 包含非 Challenge 样本")
        if not isinstance(challenge.get("challenge_task_id"), str) or not challenge["challenge_task_id"]:
            raise ScreenedCanonicalError("P2 challenge_task_id 不能为空")
    elif challenge is not None:
        raise ScreenedCanonicalError("P0/P1 challenge 必须为 null")

    quality = record.get("quality")
    if not isinstance(quality, Mapping) or set(quality) != STAGE1_FIELDS | STAGE2_FIELDS:
        raise ScreenedCanonicalError("screened canonical quality 字段不一致")
    stage1 = validate_stage1_result({key: quality[key] for key in STAGE1_FIELDS})
    if not stage1_passes(stage1):
        raise ScreenedCanonicalError("screened canonical 包含未通过第一阶段的样本")
    stage2 = validate_stage2_result({key: quality[key] for key in STAGE2_FIELDS}, stage1)
    labels = record.get("delivery_labels")
    if (not isinstance(labels, Mapping)
            or set(labels) != {"intent_label", "executable"}
            or labels.get("intent_label") != stage2["intent_label"]
            or type(labels.get("executable")) is not int
            or labels["executable"] not in (0, 1)):
        raise ScreenedCanonicalError("screened canonical 下游标签非法")

    selection = record.get("selection")
    selection_fields = {
        "origin", "round_id", "candidate_task_id", "stage1_task_id", "stage2_task_id"
    }
    if not isinstance(selection, Mapping) or set(selection) != selection_fields:
        raise ScreenedCanonicalError("screened canonical selection 字段不一致")
    origin = selection.get("origin")
    round_id = selection.get("round_id")
    if origin not in SELECTION_ORIGINS:
        raise ScreenedCanonicalError("selection.origin 非法")
    if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id < 0:
        raise ScreenedCanonicalError("selection.round_id 非法")
    if (origin == "initial_wave") != (round_id == 0):
        raise ScreenedCanonicalError("selection origin 与 round_id 不一致")
    if set_name == "P0" and origin != "initial_wave":
        raise ScreenedCanonicalError("P0 不能包含补采样本")
    for key in ("candidate_task_id", "stage1_task_id", "stage2_task_id"):
        value = selection.get(key)
        if not isinstance(value, str) or not value:
            raise ScreenedCanonicalError(f"selection.{key} 不能为空")
    expected_candidate_task = (
        f"{set_name}:{round_id}:{candidate['source_identity_sha256']}"
    )
    if selection["candidate_task_id"] != expected_candidate_task:
        raise ScreenedCanonicalError("candidate_task_id 与样本身份不一致")


def validate_screened_canonical_records(
    set_name: str,
    records: Sequence[Mapping[str, Any]],
    *,
    expected_candidate_tasks: Mapping[str, Mapping[str, Any]] | None = None,
    expected_final_count: int | None = None,
) -> None:
    """Validate one complete branch and optionally reconcile frozen tasks/counts."""
    if set_name not in SET_NAMES:
        raise ScreenedCanonicalError("未知数据集")
    if expected_final_count is not None and len(records) != expected_final_count:
        raise ScreenedCanonicalError("screened canonical 数量与构建汇总不一致")
    seen: set[str] = set()
    for record in records:
        validate_screened_canonical_record(record)
        if record["set"] != set_name:
            raise ScreenedCanonicalError("screened canonical 分支归属不一致")
        identity = str(record["candidate"]["source_identity_sha256"])
        if identity in seen:
            raise ScreenedCanonicalError("screened canonical 集合内 identity 重复")
        seen.add(identity)
        if expected_candidate_tasks is not None:
            task_id = str(record["selection"]["candidate_task_id"])
            task = expected_candidate_tasks.get(task_id)
            if task is None:
                raise ScreenedCanonicalError("screened canonical 无法关联候选任务")
            if (
                task.get("set") != set_name
                or task.get("source_identity_sha256") != identity
                or task.get("round_id") != record["selection"]["round_id"]
                or task.get("origin") != record["selection"]["origin"]
            ):
                raise ScreenedCanonicalError("screened canonical 与候选任务不一致")


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None
