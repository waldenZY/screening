"""Deterministic direct-test flag and intent projected exclusively from Stage1."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from ..shared.risk_keywords import risk_matches
from ..quality.stage1 import validate_stage1_result, mapped_intent_label

REASON_ORDER = ("not_admitted", "not_direct", "risk_keyword", "multi_turn", "too_short")

def executable_reasons(candidate: Mapping[str, Any], stage1: Mapping[str, Any], risk_snapshot: Mapping[str, Any]) -> list[str]:
    screening = validate_stage1_result(dict(stage1))["stage1"]["screening"]
    conversation = candidate["conversation"]
    found = {
        "not_admitted": not screening["admitted"],
        "not_direct": screening["route"] != "direct",
        "risk_keyword": risk_matches(conversation["target_query"], risk_snapshot),
        "multi_turn": conversation["sample_type"] != "single_turn",
        "too_short": len(conversation["target_query"]) <= 2,
    }
    return [name for name in REASON_ORDER if found[name]]

def delivery_labels(candidate: Mapping[str, Any], stage1: Mapping[str, Any], risk_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    reasons = executable_reasons(candidate, stage1, risk_snapshot)
    return {"intent_label": mapped_intent_label(stage1["stage1"]["features"]), "executable": int(not reasons), "executable_reasons": reasons}
