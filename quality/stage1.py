"""Text observations, independent category review and deterministic admission.

Model observations deliberately contain no admission labels. Policy can be
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


STAGE1_FIELDS = frozenset({"stage1"})
FEATURE_FIELDS = frozenset({
    "text_state", "anchor", "goal", "objects", "constraints", "open_points", "dependencies",
})
RUNTIME_CAPABILITIES = ("public_information", "system_app_state", "current_screen_media", "identified_device_resource")
DEPENDENCY_SOURCES = frozenset({"provided", "user_to_specify", "missing_material", "missing_context", "unknown", *RUNTIME_CAPABILITIES})
OPEN_POINT_KINDS = frozenset({"intent", "action", "parameter", "object", "conflict"})
REASON_ORDER = (
    "unrecoverable_text", "no_anchor", "missing_material", "missing_context", "unknown_dependency",
    "runtime_unavailable", "missing_goal", "route_disabled", "category_not_provided", "category_partial", "category_mismatch",
)
STAGE1_BLOCKING_ISSUES = REASON_ORDER
DEFAULT_POLICY = {
    "version": 1,
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
    if type(value["version"]) is not int or value["version"] != 1:
        raise QualityResponseError("screening_policy.version 必须为 1")
    if type(value["require_category_exact"]) is not bool:
        raise QualityResponseError("require_category_exact 必须为布尔值")
    for field, allowed in (("allowed_routes", {"direct", "clarification"}), ("runtime_capabilities", set(RUNTIME_CAPABILITIES))):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(x, str) or x not in allowed for x in items) or len(items) != len(set(items)):
            raise QualityResponseError(f"screening_policy.{field} 非法")
    return copy.deepcopy(value)


def validate_features(value: Any, turns: Sequence[str] | None = None) -> dict:
    _keys(value, FEATURE_FIELDS, "Stage1 features")
    _enum(value["text_state"], {"recoverable", "broken"}, "text_state")
    _enum(value["anchor"], {"task", "topic", "dialogue", "none"}, "anchor")
    if value["goal"] is not None:
        _string(value["goal"], "goal")
    for field in ("objects", "constraints"):
        if not isinstance(value[field], list):
            raise QualityResponseError(f"{field} 必须是数组")
        for item in value[field]:
            _string(item, field)
    for field, keys in (("open_points", {"kind", "description"}),
                        ("dependencies", {"source", "description", "locator"})):
        if not isinstance(value[field], list):
            raise QualityResponseError(f"{field} 必须是数组")
        for item in value[field]:
            _keys(item, keys, field)
            _string(item["description"], "description")
            if field == "open_points":
                _enum(item["kind"], OPEN_POINT_KINDS, "open point kind")
            else:
                _enum(item["source"], DEPENDENCY_SOURCES, "dependency source")
                if item["locator"] is not None:
                    _string(item["locator"], "locator")
                if item["source"] in RUNTIME_CAPABILITIES and item["locator"] is None:
                    raise QualityResponseError("运行时依赖必须给出有文本依据的定位条件")
            if (field == "dependencies" and item["source"] in RUNTIME_CAPABILITIES
                    and turns is not None and not any(item["locator"] in turn for turn in turns)):
                raise QualityResponseError("运行时 locator 必须是输入原文中的选择条件")
    if value["anchor"] in {"none", "topic"} and value["goal"] is not None:
        raise QualityResponseError("没有具体任务或交流诉求时不能补造 goal")
    return copy.deepcopy(value)


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
    if features["text_state"] == "broken":
        reasons.add("unrecoverable_text")
    if features["anchor"] == "none":
        reasons.add("no_anchor")
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
    elif features["anchor"] == "topic" or features["open_points"] or any(d["source"] == "user_to_specify" for d in features["dependencies"]):
        route = "clarification"
    elif features["goal"] is not None:
        route = "direct"
    else:
        route = "excluded"
        reasons.add("missing_goal")
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


def build_stage1_result(features: Any, category: Any, policy: Any = None, *, turns: Sequence[str] | None = None) -> dict:
    features = validate_features(features, turns)
    category = validate_category(category)
    policy = validate_policy(DEFAULT_POLICY if policy is None else policy)
    return {"stage1": {"features": features, "category": category, "policy": policy, "screening": screen(features, category, policy)}}


def validate_stage1_result(value: Any, turns: Sequence[str] | None = None) -> dict:
    _keys(value, STAGE1_FIELDS, "Stage1 result")
    inner = value["stage1"]
    _keys(inner, {"features", "category", "policy", "screening"}, "Stage1 result")
    expected = build_stage1_result(inner["features"], inner["category"], inner["policy"], turns=turns)
    if value != expected or type(inner["screening"].get("admitted")) is not bool:
        raise QualityResponseError("Stage1 筛选结果与冻结策略不一致")
    return expected


def stage1_passes(value: Mapping[str, Any]) -> bool:
    return validate_stage1_result(dict(value))["stage1"]["screening"]["admitted"]


def stage1_usable(value: Mapping[str, Any]) -> bool:
    return value["stage1"]["screening"]["route"] != "excluded"


def category_fit(value: Mapping[str, Any]) -> str:
    return value["stage1"]["category"]["fit"] or "not_provided"


def stage1_issue_summary(results: Sequence[Mapping[str, Any]]) -> dict:
    values = [validate_stage1_result(dict(v))["stage1"] for v in results]
    counts = Counter(reason for v in values for reason in v["screening"]["reasons"])
    rejected = Counter("/".join(v["screening"]["reasons"]) for v in values if not v["screening"]["admitted"])
    return {
        "stage1_blocking_counts": {key: counts[key] for key in REASON_ORDER},
        "stage1_route_counts": {route: sum(v["screening"]["route"] == route for v in values) for route in ("direct", "clarification", "excluded")},
        "stage1_admitted_route_counts": {route: sum(v["screening"]["admitted"] and v["screening"]["route"] == route for v in values) for route in ("direct", "clarification")},
        "stage1_route_admission_rates": {
            route: _route_rate(values, route) for route in ("direct", "clarification")
        },
        "stage1_rejection_combinations": dict(sorted(rejected.items())),
    }


def _route_rate(values: Sequence[Mapping[str, Any]], route: str) -> dict:
    denominator = sum(v["screening"]["route"] == route for v in values)
    numerator = sum(v["screening"]["admitted"] and v["screening"]["route"] == route for v in values)
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 6) if denominator else None}
