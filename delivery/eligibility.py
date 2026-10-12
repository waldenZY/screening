"""Deterministic downstream labels; never changes set membership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..shared.risk_keywords import risk_matches


REASON_ORDER = (
    "risk_keyword", "multi_turn", "no_evaluable_goal", "too_short",
    "attachment_dependency", "unresolved_reference", "other_unfit",
)


def delivery_labels(
    candidate: Mapping[str, Any], stage2: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    conversation = candidate["conversation"]
    target = conversation["target_query"]
    semantic = set(stage2["execution_issues"])
    found = {
        "risk_keyword": risk_matches(target, risk_snapshot),
        "multi_turn": conversation["sample_type"] == "multi_turn",
        "no_evaluable_goal": "no_evaluable_goal" in semantic,
        "too_short": len(target) <= 2,
        "attachment_dependency": "attachment_dependency" in semantic,
        "unresolved_reference": "unresolved_reference" in semantic,
        "other_unfit": "other_unfit" in semantic,
    }
    reasons = [name for name in REASON_ORDER if found[name]]
    return {"intent_label": stage2["intent_label"], "executable": int(not reasons),
            "execution_reasons": reasons}
