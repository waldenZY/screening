"""Text observations, independent category review and deterministic admission.

Model observations contain intent labels but no admission decisions. Policy can be
replayed without an LLM and the recorded policy is checked on every read.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from typing import Any, Mapping, Sequence


class QualityResponseError(ValueError):
    """A provider response or frozen result violates its contract."""


ADMISSION_FIELDS = frozenset({"admission"})
FEATURE_FIELDS = frozenset({
    "intent_label", "intent_reason", "open_points", "dependencies",
})
INTENT_LABELS = frozenset({"task", "dialogue", "undetermined"})
ADMISSION_GROUPS = ("dialogue_without_open_points", "dialogue_with_open_points", "task_direct", "task_clarification", "excluded")
RUNTIME_CAPABILITIES = ("public_information", "system_app_state", "current_screen_media", "identified_device_resource")
DEPENDENCY_SOURCES = frozenset({"provided", "missing_material", "missing_context", "unknown", *RUNTIME_CAPABILITIES})
OPEN_POINT_KINDS = frozenset({"intent", "action", "parameter", "object", "conflict"})
REASON_ORDER = (
    "undetermined_intent", "missing_material", "missing_context", "unknown_dependency",
    "runtime_unavailable", "route_disabled", "category_not_provided", "category_partial", "category_mismatch",
)
ADMISSION_BLOCKING_ISSUES = REASON_ORDER
DEFAULT_POLICY = {
    "version": 2,
    "allowed_routes": ["direct", "clarification"],
    "require_category_exact": True,
    "runtime_capabilities": list(RUNTIME_CAPABILITIES),
}


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _keys(value: Any, fields: set | frozenset, label: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise QualityResponseError(f"{label} 字段不符合契约")


def _string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise QualityResponseError(f"{label} 必须是非空文本")


def _enum(value: Any, allowed: set | frozenset | tuple, label: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise QualityResponseError(f"{label} 枚举非法")


def validate_policy(value: Any) -> dict:
    _keys(value, set(DEFAULT_POLICY), "screening_policy")
    if type(value["version"]) is not int or value["version"] != 2:
        raise QualityResponseError("screening_policy.version 必须为 2")
    if type(value["require_category_exact"]) is not bool:
        raise QualityResponseError("require_category_exact 必须为布尔值")
    for field, allowed in (("allowed_routes", {"direct", "clarification"}), ("runtime_capabilities", set(RUNTIME_CAPABILITIES))):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(x, str) or x not in allowed for x in items) or len(items) != len(set(items)):
            raise QualityResponseError(f"screening_policy.{field} 非法")
    return copy.deepcopy(value)


def validate_features(value: Any) -> dict:
    _keys(value, FEATURE_FIELDS, "准入评估 features")
    _enum(value["intent_label"], INTENT_LABELS, "intent_label")
    _string(value["intent_reason"], "intent_reason")
    for field, keys in (("open_points", {"kind", "description"}),
                        ("dependencies", {"source", "description"})):
        if not isinstance(value[field], list):
            raise QualityResponseError(f"{field} 必须是数组")
        for item in value[field]:
            _keys(item, keys, field)
            _string(item["description"], "description")
            if field == "open_points":
                _enum(item["kind"], OPEN_POINT_KINDS, "open point kind")
            else:
                _enum(item["source"], DEPENDENCY_SOURCES, "dependency source")
    return copy.deepcopy(value)


def mapped_intent_label(features: Mapping[str, Any]) -> str:
    """The only projection from observed intent to the delivery vocabulary."""
    label = validate_features(dict(features))["intent_label"]
    if label == "undetermined":
        raise QualityResponseError("undetermined 不得映射为交付 intent_label")
    return {"dialogue": "qa", "task": "task"}[label]


def validate_category(value: Any) -> dict:
    _keys(value, {"status", "fit"}, "category review")
    _enum(value["status"], {"reviewed", "not_provided"}, "category status")
    if value["status"] == "reviewed":
        _enum(value["fit"], {"exact", "partial", "mismatch"}, "category fit")
    elif value["fit"] is not None:
        raise QualityResponseError("未提供类别时 fit 必须为空")
    return dict(value)


def validate_category_response(value: Any) -> dict:
    _keys(value, {"category_fit"}, "category response")
    return validate_category({"status": "reviewed", "fit": value["category_fit"]})


def screen(features: Mapping[str, Any], category: Mapping[str, Any], policy: Mapping[str, Any]) -> dict:
    """Hard dependencies precede any opportunity to clarify; never repairs data."""
    features, category, policy = validate_features(dict(features)), validate_category(dict(category)), validate_policy(dict(policy))
    reasons = set()
    if features["intent_label"] == "undetermined":
        reasons.add("undetermined_intent")
    for dep in features["dependencies"]:
        source = dep["source"]
        if source in {"missing_material", "missing_context"}:
            reasons.add(source)
        elif source == "unknown":
            reasons.add("unknown_dependency")
        elif source in RUNTIME_CAPABILITIES and source not in policy["runtime_capabilities"]:
            reasons.add("runtime_unavailable")
    if reasons:
        route = "excluded"
    elif features["intent_label"] == "task" and features["open_points"]:
        route = "clarification"
    else:
        route = "direct"
    if route != "excluded" and route not in policy["allowed_routes"]:
        reasons.add("route_disabled")
    if policy["require_category_exact"]:
        if category["status"] == "not_provided":
            reasons.add("category_not_provided")
        elif category["fit"] != "exact":
            reasons.add(f"category_{category['fit']}")
    return {
        "route": route,
        "admitted": not reasons,
        "reasons": [x for x in REASON_ORDER if x in reasons],
        "policy_sha256": stable_hash(policy),
    }


def build_admission_result(features: Any, category: Any, policy: Any = None) -> dict:
    features = validate_features(features)
    category = validate_category(category)
    policy = validate_policy(DEFAULT_POLICY if policy is None else policy)
    return {"admission": {"features": features, "category": category, "policy": policy, "screening": screen(features, category, policy)}}


def validate_admission_result(value: Any) -> dict:
    _keys(value, ADMISSION_FIELDS, "准入评估 result")
    inner = value["admission"]
    _keys(inner, {"features", "category", "policy", "screening"}, "准入评估 result")
    expected = build_admission_result(inner["features"], inner["category"], inner["policy"])
    if value != expected or type(inner["screening"].get("admitted")) is not bool:
        raise QualityResponseError("准入评估 筛选结果与冻结策略不一致")
    return expected


def admission_passes(value: Mapping[str, Any]) -> bool:
    return validate_admission_result(dict(value))["admission"]["screening"]["admitted"]


def admission_usable(value: Mapping[str, Any]) -> bool:
    return value["admission"]["screening"]["route"] != "excluded"


def category_fit(value: Mapping[str, Any]) -> str:
    return value["admission"]["category"]["fit"] or "not_provided"


def admission_group(value: Mapping[str, Any]) -> str:
    inner = validate_admission_result(dict(value))["admission"]
    if not inner["screening"]["admitted"]:
        return "excluded"
    features = inner["features"]
    if features["intent_label"] == "dialogue":
        return "dialogue_with_open_points" if features["open_points"] else "dialogue_without_open_points"
    return "task_clarification" if inner["screening"]["route"] == "clarification" else "task_direct"


def admission_issue_summary(results: Sequence[Mapping[str, Any]]) -> dict:
    values = [validate_admission_result(dict(v))["admission"] for v in results]
    counts = Counter(reason for v in values for reason in v["screening"]["reasons"])
    rejected = Counter("/".join(v["screening"]["reasons"]) for v in values if not v["screening"]["admitted"])
    groups = Counter(admission_group({"admission": v}) for v in values)
    intents = Counter(v["features"]["intent_label"] for v in values)
    return {
        "admission_groups": {key: groups[key] for key in ADMISSION_GROUPS},
        "admission_intent_counts": {key: intents[key] for key in sorted(INTENT_LABELS)},
        "admission_blocking_counts": {key: counts[key] for key in REASON_ORDER},
        "admission_route_counts": {route: sum(v["screening"]["route"] == route for v in values) for route in ("direct", "clarification", "excluded")},
        "admission_admitted_route_counts": {route: sum(v["screening"]["admitted"] and v["screening"]["route"] == route for v in values) for route in ("direct", "clarification")},
        "admission_route_admission_rates": {
            route: _route_rate(values, route) for route in ("direct", "clarification")
        },
        "admission_rejection_combinations": dict(sorted(rejected.items())),
    }


def _route_rate(values: Sequence[Mapping[str, Any]], route: str) -> dict:
    denominator = sum(v["screening"]["route"] == route for v in values)
    numerator = sum(v["screening"]["admitted"] and v["screening"]["route"] == route for v in values)
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 6) if denominator else None}
