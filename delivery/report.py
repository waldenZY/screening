"""Deterministic, sanitized run-level reporting for screening v4."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..quality.stage1 import stage1_usable, category_fit
from .eligibility import REASON_ORDER as EXECUTABLE_REASONS
from ..candidate.p2_adaptive import P2_ALGORITHM, P2_SOURCE_NAMES
from ..quality.runner import CHALLENGE_TYPES
from ..quality.stage1 import (STAGE1_FIELDS, STAGE1_BLOCKING_ISSUES, ADMISSION_GROUPS, INTENT_LABELS, stage1_issue_summary, validate_stage1_result, stage1_passes)
from ..shared.json_io import JsonObject, write_json
from ..shared.taxonomy import TaxonomyMappings
from .screened import validate_screened_canonical_record


RUN_REPORT_SCHEMA_VERSION = 13
RUN_REPORT_JSON = "screening_run_report.json"
RUN_REPORT_MARKDOWN = "screening_run_report.md"
SET_NAMES = ("P0", "P1", "P2")
_CATEGORY_FIT_VALUES = ("exact", "partial", "mismatch", "not_provided")
_SENSITIVE_KEYS = frozenset({"text", "raw_text", "target_query", "context_turns", "conversation_turns", "full_text"})


class RunReportError(ValueError):
    """A deterministic report contract or reconciliation failure."""


def _ratio(numerator: int, denominator: int) -> JsonObject:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise RunReportError("报告比例的分子或分母非法")
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": None if denominator == 0 else round(numerator / denominator, 6),
    }


def _is_usable(stage1: Mapping[str, Any]) -> bool:
    return stage1_usable(stage1)


def _l1_for_l2(taxonomy: TaxonomyMappings, l2_key: str) -> str:
    matches = [l1 for l1 in taxonomy.l1_order if l2_key.startswith(f"{l1}/")]
    if not matches:
        raise RunReportError(f"报告无法定位 L2 所属 L1: {l2_key}")
    return max(matches, key=len)


def _l2_name(l1: str, l2_key: str) -> str:
    prefix = f"{l1}/"
    if not l2_key.startswith(prefix) or len(l2_key) == len(prefix):
        raise RunReportError(f"报告 L2 key 非法: {l2_key}")
    return l2_key[len(prefix):]


def _empty_counts() -> dict[str, Any]:
    return {
        "stage1_evaluated": 0,
        "usable_queries": 0,
        "category_exact_among_usable": 0,
        "stage1_pass": 0,
        "final_count": 0,
        "category_fit": Counter(),
    }


def _add_stage1(counts: dict[str, Any], result: Mapping[str, Any]) -> None:
    counts["stage1_evaluated"] += 1
    counts["category_fit"][category_fit(result)] += 1
    usable = _is_usable(result)
    if usable:
        counts["usable_queries"] += 1
        if category_fit(result) == "exact":
            counts["category_exact_among_usable"] += 1
    if stage1_passes(result):
        counts["stage1_pass"] += 1


def _finalize_counts(counts: Mapping[str, Any]) -> JsonObject:
    evaluated = int(counts["stage1_evaluated"])
    usable = int(counts["usable_queries"])
    exact = int(counts["category_exact_among_usable"])
    passed = int(counts["stage1_pass"])
    return {
        "stage1_evaluated": evaluated,
        "usable_queries": usable,
        "query_usability_rate": _ratio(usable, evaluated),
        "category_fit_distribution": {
            value: int(counts["category_fit"].get(value, 0))
            for value in _CATEGORY_FIT_VALUES
        },
        "category_exact_among_usable": exact,
        "semantic_label_fit_rate": _ratio(exact, usable),
        "stage1_pass": passed,
        "stage1_admission_rate": _ratio(passed, evaluated),
        "final_count": int(counts["final_count"]),
    }


def _distribution(
    values: Counter[str],
    allowed: Sequence[str] | None = None,
) -> JsonObject:
    keys = list(allowed) if allowed is not None else sorted(values)
    return {key: int(values.get(key, 0)) for key in keys}


def _p1_diagnostics(build_summary: Mapping[str, Any]) -> JsonObject:
    supplement = build_summary.get("p1_supplement")
    if not isinstance(supplement, Mapping):
        raise RunReportError("P1 补采诊断缺少汇总")
    l2_totals: dict[str, Counter[str]] = defaultdict(Counter)
    cluster_totals: dict[tuple[str, Any], Counter[str]] = defaultdict(Counter)
    round_rows: list[JsonObject] = []
    for round_row in supplement["rounds"]:
        batches = round_row["batches"]
        round_usable = 0
        round_exact = 0
        for key, count in round_row["requested_by_l2"].items():
            l2_totals[key]["requested"] += int(count)
        for batch in batches:
            for key, row in batch["l2"].items():
                for metric in ("sampled", "evaluated", "usable", "exact", "stage1_pass", "admitted"):
                    l2_totals[key][metric] += int(row[metric])
                round_usable += int(row["usable"])
                round_exact += int(row["exact"])
            for row in batch["clusters"]:
                key = (str(row["l2_key"]), row["cluster_id"])
                for metric in ("sampled", "evaluated", "usable", "exact", "stage1_pass", "admitted"):
                    cluster_totals[key][metric] += int(row[metric])
        if sum(int(batch["sampled"]) for batch in batches) != int(round_row["sampled"]):
            raise RunReportError("P1 补采小批与轮次抽样数不守恒")
        round_rows.append({
            "round": int(round_row["round"]),
            "batch_count": len(batches),
            "planned_request": sum(int(value) for value in round_row["requested_by_l2"].values()),
            "sampled": int(round_row["sampled"]),
            "evaluated": int(round_row["after_dedup"]),
            "usable": round_usable,
            "exact": round_exact,
            "stage1_pass": int(round_row["stage1_pass"]),
            "admitted": int(round_row["admitted"]),
            "stop_reason": round_row["stop"],
        })
    quotas = supplement["target_quotas_by_l2"]
    deficits = supplement["deficits_by_l2"]
    remaining = supplement["candidate_remaining_by_l2"]
    stops = supplement["stop_reason_by_l2"]
    l2_rows = [
        {
            "l2_key": key,
            "quota": int(quotas[key]),
            "deficit": int(deficits[key]),
            "candidate_remaining": int(remaining[key]),
            "stop_reason": stops[key],
            **{metric: int(l2_totals[key][metric]) for metric in
               ("requested", "sampled", "evaluated", "usable", "exact", "stage1_pass", "admitted")},
        }
        for key in sorted(quotas)
    ]
    cluster_rows = [
        {"l2_key": key[0], "cluster_id": key[1],
         **{metric: int(counts[metric]) for metric in
            ("sampled", "evaluated", "usable", "exact", "stage1_pass", "admitted")}}
        for key, counts in cluster_totals.items()
    ]
    deficit_rows = [row for row in l2_rows if row["deficit"] > 0]
    small_deficits = [row for row in deficit_rows if row["deficit"] <= 2]
    return {
        "detail_path": "P1/supplement_summary.json",
        "rounds": round_rows,
        "l2_with_deficit": len(deficit_rows),
        "small_deficit_l2": len(small_deficits),
        "small_deficit_total": sum(row["deficit"] for row in small_deficits),
        "top_deficit_l2": sorted(deficit_rows, key=lambda row: (-row["deficit"], row["l2_key"]))[:10],
        "top_unusable_clusters": sorted(
            cluster_rows,
            key=lambda row: (-(row["evaluated"] - row["usable"]), row["l2_key"], str(row["cluster_id"])),
        )[:10],
        "top_category_mismatch_clusters": sorted(
            cluster_rows,
            key=lambda row: (-(row["usable"] - row["exact"]), row["l2_key"], str(row["cluster_id"])),
        )[:10],
    }


def _p2_diagnostics(
    build_summary: Mapping[str, Any],
    final_records: Sequence[Mapping[str, Any]],
    evaluated_candidates: Sequence[Mapping[str, Any]],
    challenge_runtime: Mapping[str, Any] | None,
) -> JsonObject:
    value = build_summary.get("p2_adaptive")
    if not isinstance(value, Mapping):
        raise RunReportError("P2 自适应诊断缺少汇总")
    primary = Counter()
    labels = Counter()
    combinations = Counter()
    frequency_bands = Counter()
    candidates_by_identity = {
        str(candidate["source_identity_sha256"]): candidate
        for candidate in evaluated_candidates
    }
    for record in final_records:
        challenge = record.get("challenge")
        if not isinstance(challenge, Mapping) or challenge.get("is_challenge") is not True:
            raise RunReportError("P2 最终记录缺少 Challenge 结果")
        primary[str(challenge["primary_challenge"])] += 1
        identity = str(record["candidate"]["source_identity_sha256"])
        if identity not in candidates_by_identity:
            raise RunReportError("P2 最终记录缺少候选采样特征")
        frequency_bands[str(candidates_by_identity[identity]["features"]["frequency_band"])] += 1
        types = [str(item) for item in challenge["challenge_types"]]
        labels.update(types)
        combinations["+".join(types)] += 1
    selected = value.get("selection", {})
    return {
        "algorithm": value.get("algorithm"),
        "rounds": list(value.get("rounds", [])),
        "source_funnel": dict(value.get("source_funnel", {})),
        "candidate_remaining_by_source": dict(value.get("candidate_remaining_by_source", {})),
        "final_by_primary": {key: int(primary[key]) for key in CHALLENGE_TYPES},
        "final_share_by_primary": {
            key: (0.0 if not final_records else round(primary[key] / len(final_records), 6))
            for key in CHALLENGE_TYPES
        },
        "soft_min_deficit_by_primary": dict(selected.get("soft_min_deficit_by_primary", {})),
        "challenge_type_distribution": {key: int(labels[key]) for key in CHALLENGE_TYPES},
        "challenge_type_combinations": dict(sorted(combinations.items(), key=lambda item: (-item[1], item[0]))),
        "final_frequency_band_distribution": dict(sorted(frequency_bands.items())),
        "judge_pass_not_selected": int(selected.get("judge_pass_not_selected", 0)),
        "not_selected_reasons": dict(selected.get("not_selected_reasons", {})),
        "phase_duration_ms": dict(value.get("phase_duration_ms", {})),
        "target": int(value.get("target", 0)),
        "soft_min_by_primary": {
            key: int(dict(value.get("soft_min_by_primary", {})).get(key, 0))
            for key in CHALLENGE_TYPES
        },
        "max_per_primary": int(value.get("max_per_primary", 0)),
        "final": int(value.get("final", 0)),
        "target_gap": int(value.get("target_gap", 0)),
        "stop_reason": value.get("stop_reason"),
        "candidate_exhausted": bool(value.get("candidate_exhausted", False)),
        "budget": dict(value.get("budget", {})),
        "challenge_runtime": dict(challenge_runtime or {}),
    }


def build_run_report(
    *,
    run_id: str,
    taxonomy: TaxonomyMappings,
    evaluated_candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    evaluation_rounds: Mapping[str, Mapping[str, int]],
    stage1_results: Mapping[str, Mapping[str, Any]],
    final_records: Mapping[str, Sequence[Mapping[str, Any]]],
    p1_target_quotas: Mapping[str, int],
    build_summary: Mapping[str, Any],
    challenge_runtime: Mapping[str, Any] | None = None,
) -> JsonObject:
    """Build a complete report without retaining any query text."""
    if set(evaluated_candidates) != set(SET_NAMES) or set(final_records) != set(SET_NAMES):
        raise RunReportError("报告必须同时包含 P0/P1/P2")
    if set(evaluation_rounds) != set(SET_NAMES):
        raise RunReportError("报告候选轮次必须同时包含 P0/P1/P2")

    l2_to_l1 = {
        l2_key: _l1_for_l2(taxonomy, l2_key)
        for l2_key in taxonomy.l2_population
    }
    sets: JsonObject = {}
    total_final = 0

    for set_name in SET_NAMES:
        set_counts = _empty_counts()
        node_counts = {key: _empty_counts() for key in taxonomy.l2_population}
        wave_counts: dict[int, dict[str, Any]] = defaultdict(_empty_counts)
        seen_identities: set[str] = set()

        for candidate in evaluated_candidates[set_name]:
            identity = str(candidate["source_identity_sha256"])
            if identity in seen_identities:
                raise RunReportError(f"{set_name} 报告候选 identity 重复")
            seen_identities.add(identity)
            taxonomy_value = candidate["taxonomy"]
            l2_key = str(taxonomy_value["l2_key"])
            if l2_key not in node_counts:
                raise RunReportError(f"{set_name} 报告候选 L2 不在 taxonomy")
            if taxonomy_value["l1"] != l2_to_l1[l2_key]:
                raise RunReportError(f"{set_name} 报告候选 L1/L2 不一致")
            result = validate_stage1_result(dict(stage1_results[identity]))
            round_id = evaluation_rounds[set_name].get(identity)
            if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id < 0:
                raise RunReportError(f"{set_name} 报告候选缺少合法轮次")
            if set_name == "P0" and round_id != 0:
                raise RunReportError(f"{set_name} 不得出现补采轮次")
            _add_stage1(set_counts, result)
            _add_stage1(node_counts[l2_key], result)
            _add_stage1(wave_counts[round_id], result)

        final_round_counts: Counter[int] = Counter()
        intent_labels: Counter[str] = Counter()
        executable_values: Counter[str] = Counter()
        zero_reasons: Counter[str] = Counter()
        final_l2_counts: Counter[str] = Counter()
        for record in final_records[set_name]:
            validate_screened_canonical_record(record)
            if record["set"] != set_name:
                raise RunReportError(f"{set_name} 报告混入其他集合记录")
            candidate = record["candidate"]
            identity = str(candidate["source_identity_sha256"])
            if identity not in seen_identities:
                raise RunReportError(f"{set_name} 最终记录未出现在候选统计中")
            l2_key = str(candidate["taxonomy"]["l2_key"])
            node_counts[l2_key]["final_count"] += 1
            set_counts["final_count"] += 1
            final_l2_counts[l2_key] += 1
            round_id = int(record["selection"]["round_id"])
            final_round_counts[round_id] += 1
            quality = record["quality"]
            validate_stage1_result(
                {
                    key: quality[key]
                    for key in STAGE1_FIELDS
                }
            )
            intent_labels[quality["intent_label"]] += 1
            executable_values[str(record["delivery_labels"]["executable"])] += 1
            zero_reasons.update(record["delivery_labels"]["executable_reasons"])

        target_scope = {
            key for key, value in p1_target_quotas.items() if int(value) > 0
        } if set_name == "P1" else {
            key
            for key, value in node_counts.items()
            if int(value["stage1_evaluated"]) > 0
        }
        covered_l2 = {key for key, count in final_l2_counts.items() if count > 0}
        covered_l1 = {l2_to_l1[key] for key in covered_l2}
        target_covered = covered_l2 & target_scope

        l2_rows: list[JsonObject] = []
        for l2_key, population in taxonomy.l2_population.items():
            l1 = l2_to_l1[l2_key]
            row = {
                "l1": l1,
                "l2": _l2_name(l1, l2_key),
                "l2_key": l2_key,
                "taxonomy_population": int(population),
                **_finalize_counts(node_counts[l2_key]),
                "final_share": (
                    None
                    if set_counts["final_count"] == 0
                    else round(node_counts[l2_key]["final_count"] / set_counts["final_count"], 6)
                ),
                "target_quota": (
                    int(p1_target_quotas.get(l2_key, 0)) if set_name == "P1" else None
                ),
                "deficit": (
                    max(
                        0,
                        int(p1_target_quotas.get(l2_key, 0))
                        - int(node_counts[l2_key]["final_count"]),
                    )
                    if set_name == "P1"
                    else None
                ),
                "in_coverage_scope": l2_key in target_scope,
            }
            l2_rows.append(row)

        tree: list[JsonObject] = []
        for l1 in taxonomy.l1_order:
            children = [row for row in l2_rows if row["l1"] == l1]
            l1_counts = _empty_counts()
            for child in children:
                l1_counts["stage1_evaluated"] += child["stage1_evaluated"]
                l1_counts["usable_queries"] += child["usable_queries"]
                l1_counts["category_exact_among_usable"] += child["category_exact_among_usable"]
                l1_counts["stage1_pass"] += child["stage1_pass"]
                l1_counts["final_count"] += child["final_count"]
                l1_counts["category_fit"].update(child["category_fit_distribution"])
            target_children = [row for row in children if row["in_coverage_scope"]]
            covered_children = [row for row in target_children if row["final_count"] > 0]
            tree.append(
                {
                    "l1": l1,
                    "taxonomy_population": int(taxonomy.l1_population[l1]),
                    "taxonomy_l2_count": len(children),
                    "coverage_scope_l2_count": len(target_children),
                    "covered_scope_l2_count": len(covered_children),
                    "scope_l2_coverage_rate": _ratio(
                        len(covered_children), len(target_children)
                    ),
                    "target_quota": (
                        sum(int(row["target_quota"]) for row in children)
                        if set_name == "P1"
                        else None
                    ),
                    **_finalize_counts(l1_counts),
                    "l2": children,
                }
            )

        quota_fulfilled = sum(
            min(final_l2_counts.get(key, 0), int(quota))
            for key, quota in p1_target_quotas.items()
        )
        quota_total = sum(int(value) for value in p1_target_quotas.values())
        distribution_deviation: float | None = None
        if set_name == "P1" and quota_total and set_counts["final_count"]:
            distribution_deviation = round(
                0.5
                * sum(
                    abs(
                        final_l2_counts.get(key, 0) / set_counts["final_count"]
                        - int(quota) / quota_total
                    )
                    for key, quota in p1_target_quotas.items()
                ),
                6,
            )

        wave_rows = []
        for round_id in sorted(wave_counts):
            values = _finalize_counts(wave_counts[round_id])
            values.update(
                {
                    "origin": "initial_wave" if round_id == 0 else "supplement_wave",
                    "round_id": round_id,
                    "final_count": int(final_round_counts.get(round_id, 0)),
                }
            )
            wave_rows.append(values)

        set_report = {
            "coverage_scope": "p1_target_quota_l2" if set_name == "P1" else "stage1_evaluated_l2",
            **_finalize_counts(set_counts),
            **stage1_issue_summary([
                stage1_results[candidate["source_identity_sha256"]]
                for candidate in evaluated_candidates[set_name]
            ]),
            "taxonomy_l1_coverage_rate": _ratio(
                len(covered_l1), len(taxonomy.l1_order)
            ),
            "taxonomy_l2_coverage_rate": _ratio(
                len(covered_l2), len(taxonomy.l2_population)
            ),
            "scope_l2_coverage_rate": _ratio(
                len(target_covered), len(target_scope)
            ),
            "p1_quota_fulfillment_rate": (
                _ratio(quota_fulfilled, quota_total) if set_name == "P1" else None
            ),
            "p1_target_distribution_deviation": distribution_deviation,
            "waves": wave_rows,
            "executable_zero_reasons": {reason: zero_reasons[reason] for reason in EXECUTABLE_REASONS},
            "final_quality_distribution": {
                "intent_label": _distribution(intent_labels, ("task", "qa")),
                "executable": _distribution(executable_values, ("0", "1")),
            },
            "semantic_tree": tree,
        }
        sets[set_name] = set_report
        total_final += int(set_counts["final_count"])

    report: JsonObject = {
        "report_schema_version": RUN_REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "metric_notice": (
            "semantic_label_fit_rate is an LLM-as-judge agreement estimate, "
            "not a human-gold accuracy measurement"
        ),
        "metric_definitions": {
            "query_usability_rate": (
                "direct or clarification text route / all completed stage1 candidates"
            ),
            "semantic_label_fit_rate": (
                "category_fit=exact among direct or clarification text route candidates"
            ),
            "stage1_admission_rate": (
                "policy admitted / all completed stage1 candidates"
            ),
            "scope_l2_coverage_rate": (
                "final covered L2 / reporting scope L2; P1 uses frozen quota L2, "
                "P0/P2 use stage1-evaluated L2"
            ),
            "p1_quota_fulfillment_rate": (
                "sum(min(final L2 count, frozen quota)) / sum(frozen L2 quota)"
            ),
        },
        "taxonomy": {
            "l1_count": len(taxonomy.l1_order),
            "l2_count": len(taxonomy.l2_population),
            "declared_l1_population": sum(taxonomy.l1_population.values()),
            "declared_l2_population": sum(taxonomy.l2_population.values()),
        },
        "totals": {
            "final_records_across_sets": total_final,
            "cross_set_overlap": dict(build_summary.get("cross_set_overlap", {})),
        },
        "sets": sets,
        "p1_diagnostics": _p1_diagnostics(build_summary),
        "dedup": dict(build_summary["dedup"]),
        "p2_diagnostics": _p2_diagnostics(
            build_summary, final_records["P2"], evaluated_candidates["P2"], challenge_runtime
        ),
    }
    validate_run_report(report)
    return report


def _validate_ratio(value: Any, label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"numerator", "denominator", "rate"}:
        raise RunReportError(f"{label} 比例结构非法")
    numerator = value["numerator"]
    denominator = value["denominator"]
    if (
        isinstance(numerator, bool)
        or not isinstance(numerator, int)
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
        or numerator < 0
        or denominator < 0
        or numerator > denominator
    ):
        raise RunReportError(f"{label} 比例数值非法")
    expected = None if denominator == 0 else round(numerator / denominator, 6)
    if value["rate"] != expected:
        raise RunReportError(f"{label} 比例计算不一致")


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        if _SENSITIVE_KEYS & set(value):
            raise RunReportError("批跑报告不得包含 Query 正文相关字段")
        for child in value.values():
            _reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_keys(child)


def validate_run_report(report: Mapping[str, Any]) -> None:
    """Strictly validate report totals, ratios, hierarchy, and privacy."""
    expected_top = {
        "report_schema_version",
        "run_id",
        "metric_notice",
        "metric_definitions",
        "taxonomy",
        "totals",
        "sets",
        "p1_diagnostics",
        "dedup",
        "p2_diagnostics",
    }
    if not isinstance(report, Mapping) or set(report) != expected_top:
        raise RunReportError("批跑报告顶层字段不一致")
    if report.get("report_schema_version") != RUN_REPORT_SCHEMA_VERSION:
        raise RunReportError("批跑报告 schema version 非法")
    if not isinstance(report.get("run_id"), str) or not report["run_id"]:
        raise RunReportError("批跑报告 run_id 非法")
    if not isinstance(report.get("sets"), Mapping) or set(report["sets"]) != set(SET_NAMES):
        raise RunReportError("批跑报告集合不完整")
    _reject_sensitive_keys(report)
    dedup = report.get("dedup")
    if not isinstance(dedup, Mapping) or set(dedup) != set(SET_NAMES):
        raise RunReportError("Dedup report must include P0/P1/P2")
    for name, row in dedup.items():
        if (not isinstance(row, Mapping) or set(row) != {
                "exact_dropped", "near_dropped", "borderline_retained", "audit_path", "backfill_gap"}
                or any(type(row[key]) is not int or row[key] < 0 for key in
                       ("exact_dropped", "near_dropped", "borderline_retained", "backfill_gap"))
                or row["audit_path"] != f"{name}/dedup_audit.jsonl"):
            raise RunReportError("Invalid dedup report entry")
    diagnostics = report["p1_diagnostics"]
    if not isinstance(diagnostics, Mapping) or not isinstance(diagnostics.get("rounds"), list):
        raise RunReportError("P1 补采诊断结构非法")
    if (
        sum(int(row["evaluated"]) for row in diagnostics["rounds"])
        != sum(int(row["stage1_evaluated"]) for row in report["sets"]["P1"]["waves"]
           if int(row["round_id"]) > 0)
    ):
        raise RunReportError("P1 补采诊断与 Stage1 波次数量不守恒")
    p2_diagnostics = report["p2_diagnostics"]
    if not isinstance(p2_diagnostics, Mapping) or not isinstance(p2_diagnostics.get("rounds"), list):
        raise RunReportError("P2 自适应诊断结构非法")
    if p2_diagnostics.get("algorithm") != P2_ALGORITHM:
        raise RunReportError("P2 自适应算法标识非法")
    if int(p2_diagnostics.get("final", -1)) != int(report["sets"]["P2"]["final_count"]):
        raise RunReportError("P2 自适应诊断与最终数量不守恒")
    target = p2_diagnostics.get("target")
    final = p2_diagnostics.get("final")
    soft_mins = p2_diagnostics.get("soft_min_by_primary")
    type_cap = p2_diagnostics.get("max_per_primary")
    final_by_primary = p2_diagnostics.get("final_by_primary")
    deficits = p2_diagnostics.get("soft_min_deficit_by_primary")
    if (
        any(type(value) is not int or value < 0 for value in (target, final, type_cap))
        or final > target
        or p2_diagnostics.get("target_gap") != target - final
        or not isinstance(soft_mins, Mapping)
        or set(soft_mins) != set(CHALLENGE_TYPES)
        or any(type(value) is not int or value < 0 for value in soft_mins.values())
        or sum(soft_mins.values()) > target
        or any(value > type_cap for value in soft_mins.values())
        or not isinstance(final_by_primary, Mapping)
        or set(final_by_primary) != set(CHALLENGE_TYPES)
        or any(type(value) is not int or value < 0 or value > type_cap for value in final_by_primary.values())
        or sum(final_by_primary.values()) != final
        or not isinstance(deficits, Mapping)
        or set(deficits) != set(CHALLENGE_TYPES)
        or deficits != {
            key: max(0, int(soft_mins[key]) - int(final_by_primary[key]))
            for key in CHALLENGE_TYPES
        }
    ):
        raise RunReportError("P2 目标、类型配额或缺口不守恒")
    budget = p2_diagnostics.get("budget")
    budget_fields = {
        "max_sampled", "sampled", "remaining", "max_tail_sampled",
        "tail_sampled", "tail_remaining", "tail_batch_size",
        "max_stalled_batches", "stalled_batches",
    }
    if (
        not isinstance(budget, Mapping)
        or set(budget) != budget_fields
        or any(type(budget[key]) is not int or budget[key] < 0 for key in budget_fields)
        or budget["sampled"] > budget["max_sampled"]
        or budget["remaining"] != budget["max_sampled"] - budget["sampled"]
        or budget["tail_sampled"] > budget["max_tail_sampled"]
        or budget["tail_remaining"] != budget["max_tail_sampled"] - budget["tail_sampled"]
        or budget["tail_batch_size"] < 1
        or budget["max_stalled_batches"] < 1
        or budget["stalled_batches"] > budget["max_stalled_batches"]
    ):
        raise RunReportError("P2 采样预算结构非法")
    if p2_diagnostics.get("stop_reason") not in {
        "target_and_soft_mins_met", "max_sampled", "max_tail_sampled",
        "stalled_batches", "candidate_exhausted", "max_rounds",
    }:
        raise RunReportError("P2 停止原因非法")
    source_funnel = p2_diagnostics.get("source_funnel")
    if not isinstance(source_funnel, Mapping) or set(source_funnel) != set(P2_SOURCE_NAMES):
        raise RunReportError("P2 来源池漏斗结构非法")
    batches = []
    for round_value in p2_diagnostics["rounds"]:
        if not isinstance(round_value, Mapping):
            raise RunReportError("P2 轮次结构非法")
        round_batches = round_value.get("batches")
        if not isinstance(round_batches, list) or any(
            not isinstance(batch, Mapping) for batch in round_batches
        ):
            raise RunReportError("P2 轮次批次结构非法")
        batches.extend(round_batches)
        if (
            int(round_value.get("actual_sampled", -1))
            != sum(int(batch.get("sampled", -1)) for batch in round_batches)
            or int(round_value.get("used_requests", -1))
            != sum(int(batch.get("requested", -1)) for batch in round_batches)
            or int(round_value.get("planned_requests", -1))
            < int(round_value.get("actual_sampled", -1))
        ):
            raise RunReportError("P2 轮次请求数量不守恒")
        for batch in round_batches:
            selected_by_primary = batch.get("selected_by_primary")
            deficit_by_primary = batch.get("deficit_by_primary")
            sampled_budget = batch.get("sampled_budget")
            tail_budget = batch.get("tail_budget")
            stalled = batch.get("stalled_batches")
            if (
                batch.get("mode") not in {"target_fill", "coverage_tail"}
                or type(batch.get("final")) is not int
                or type(batch.get("target")) is not int
                or type(batch.get("target_gap")) is not int
                or batch["target"] != target
                or batch["target_gap"] != target - batch["final"]
                or batch.get("challenge_pass") != batch.get("judge_pass")
                or not isinstance(selected_by_primary, Mapping)
                or set(selected_by_primary) != set(CHALLENGE_TYPES)
                or any(type(value) is not int or value < 0 for value in selected_by_primary.values())
                or sum(selected_by_primary.values()) != batch["final"]
                or not isinstance(deficit_by_primary, Mapping)
                or set(deficit_by_primary) != set(CHALLENGE_TYPES)
                or deficit_by_primary != {
                    key: max(0, int(soft_mins[key]) - int(selected_by_primary[key]))
                    for key in CHALLENGE_TYPES
                }
                or not isinstance(sampled_budget, Mapping)
                or sampled_budget.get("limit") != budget["max_sampled"]
                or type(sampled_budget.get("used")) is not int
                or not 0 <= sampled_budget["used"] <= sampled_budget["limit"]
                or not isinstance(tail_budget, Mapping)
                or tail_budget.get("limit") != budget["max_tail_sampled"]
                or type(tail_budget.get("used")) is not int
                or not 0 <= tail_budget["used"] <= tail_budget["limit"]
                or not isinstance(stalled, Mapping)
                or stalled.get("limit") != budget["max_stalled_batches"]
                or type(stalled.get("used")) is not int
                or not 0 <= stalled["used"] <= stalled["limit"]
                or batch.get("next_action") not in {
                    "continue_target_fill", "continue_coverage_tail",
                    "target_and_soft_mins_met", "max_sampled", "max_tail_sampled",
                    "stalled_batches", "candidate_exhausted", "max_rounds",
                }
            ):
                raise RunReportError("P2 批次配额、预算或 next_action 结构非法")
            phase_duration = batch.get("phase_duration_ms")
            if (
                not isinstance(phase_duration, Mapping)
                or set(phase_duration) != {"planning", "stage1", "challenge", "selection"}
                or any(type(value) is not int or value < 0 for value in phase_duration.values())
            ):
                raise RunReportError("P2 批次阶段耗时结构非法")
            for scalar, by_source in (
                ("requested", "requested_by_source"),
                ("sampled", "sampled_by_source"),
                ("after_dedup", "after_dedup_by_source"),
                ("stage1_pass", "stage1_pass_by_source"),
                ("judge_pass", "judge_pass_by_source"),
            ):
                values = batch.get(by_source)
                if (
                    not isinstance(values, Mapping)
                    or any(key not in P2_SOURCE_NAMES for key in values)
                    or int(batch.get(scalar, -1)) != sum(int(value) for value in values.values())
                ):
                    raise RunReportError(f"P2 批次 {scalar} 来源数量不守恒")
    phase_duration_ms = p2_diagnostics.get("phase_duration_ms")
    if (
        not isinstance(phase_duration_ms, Mapping)
        or set(phase_duration_ms) != {"planning", "stage1", "challenge", "selection"}
        or any(type(value) is not int or value < 0 for value in phase_duration_ms.values())
        or phase_duration_ms != {
            phase: sum(int(batch["phase_duration_ms"][phase]) for batch in batches)
            for phase in ("planning", "stage1", "challenge", "selection")
        }
    ):
        raise RunReportError("P2 阶段耗时与批次不守恒")
    funnel_fields = {
        "candidate_capacity", "requested", "actual_sampled", "after_dedup",
        "stage1_pass", "judge_pass", "final",
    }
    for source in P2_SOURCE_NAMES:
        row = source_funnel[source]
        if (
            not isinstance(row, Mapping)
            or set(row) != funnel_fields
            or any(type(row[field]) is not int or row[field] < 0 for field in funnel_fields)
            or not (
                row["candidate_capacity"] >= row["actual_sampled"]
                and row["requested"] >= row["actual_sampled"]
                and row["actual_sampled"] >= row["after_dedup"]
                >= row["stage1_pass"] >= row["judge_pass"] >= row["final"]
            )
        ):
            raise RunReportError(f"P2 来源池 {source} 漏斗数量非法")
        expected = {
            "requested": sum(int(batch.get("requested_by_source", {}).get(source, 0)) for batch in batches),
            "actual_sampled": sum(int(batch.get("sampled_by_source", {}).get(source, 0)) for batch in batches),
            "after_dedup": sum(int(batch.get("after_dedup_by_source", {}).get(source, 0)) for batch in batches),
            "stage1_pass": sum(int(batch.get("stage1_pass_by_source", {}).get(source, 0)) for batch in batches),
            "judge_pass": sum(int(batch.get("judge_pass_by_source", {}).get(source, 0)) for batch in batches),
        }
        if any(int(row[key]) != value for key, value in expected.items()):
            raise RunReportError(f"P2 来源池 {source} 漏斗与批次不守恒")
    if sum(int(source_funnel[source]["final"]) for source in P2_SOURCE_NAMES) != int(p2_diagnostics["final"]):
        raise RunReportError("P2 来源池最终入集数不守恒")
    reasons = p2_diagnostics.get("not_selected_reasons")
    if (
        not isinstance(reasons, Mapping)
        or set(reasons) != {"type_cap", "total_limit", "diversity"}
        or any(type(value) is not int or value < 0 for value in reasons.values())
        or sum(reasons.values()) != int(p2_diagnostics.get("judge_pass_not_selected", -1))
    ):
        raise RunReportError("P2 Judge 通过未入集原因不守恒")

    total_final = 0
    for set_name in SET_NAMES:
        set_report = report["sets"][set_name]
        if not isinstance(set_report, Mapping):
            raise RunReportError(f"{set_name} 报告必须是对象")
        blockers = set_report.get("stage1_blocking_counts")
        evaluated = set_report.get("stage1_evaluated", 0)
        groups = set_report.get("stage1_admission_groups")
        intents = set_report.get("stage1_intent_counts")
        if (not isinstance(groups, Mapping) or set(groups) != set(ADMISSION_GROUPS)
                or any(type(v) is not int or v < 0 for v in groups.values())
                or sum(groups.values()) != evaluated
                or groups["excluded"] != evaluated - set_report.get("stage1_pass", 0)
                or not isinstance(intents, Mapping) or set(intents) != INTENT_LABELS
                or any(type(v) is not int or v < 0 for v in intents.values())
                or sum(intents.values()) != evaluated):
            raise RunReportError(f"{set_name} Stage1 目标类型或五组统计不守恒")
        if (not isinstance(blockers, Mapping) or set(blockers) != set(STAGE1_BLOCKING_ISSUES)
                or any(type(count) is not int or not 0 <= count <= evaluated for count in blockers.values())):
            raise RunReportError(f"{set_name} Stage1 阻断统计非法；多原因计数允许重叠")
        routes = set_report.get("stage1_route_counts")
        admitted_routes = set_report.get("stage1_admitted_route_counts")
        if (not isinstance(routes, Mapping) or set(routes) != {"direct", "clarification", "excluded"}
                or any(type(v) is not int or v < 0 for v in routes.values()) or sum(routes.values()) != evaluated
                or not isinstance(admitted_routes, Mapping) or set(admitted_routes) != {"direct", "clarification"}
                or any(type(v) is not int or not 0 <= v <= routes[k] for k, v in admitted_routes.items())
                or sum(admitted_routes.values()) != set_report.get("stage1_pass", 0)):
            raise RunReportError(f"{set_name} Stage1 route 统计不守恒")
        if set_report.get("stage1_route_admission_rates") != {
            route: _ratio(admitted_routes[route], routes[route]) for route in ("direct", "clarification")
        }:
            raise RunReportError(f"{set_name} Stage1 分类型准入率不一致")
        if (groups["task_clarification"] != admitted_routes["clarification"]
                or sum(groups[k] for k in ("dialogue_without_open_points", "dialogue_with_open_points", "task_direct")) != admitted_routes["direct"]):
            raise RunReportError(f"{set_name} Stage1 五组与路线统计不一致")
        rejections = set_report.get("stage1_rejection_combinations")
        if (not isinstance(rejections, Mapping)
                or any(not isinstance(key, str) or not key or any(x not in STAGE1_BLOCKING_ISSUES for x in key.split("/"))
                       or type(count) is not int or count <= 0 for key, count in rejections.items())
                or sum(rejections.values()) != evaluated - set_report.get("stage1_pass", 0)):
            raise RunReportError(f"{set_name} Stage1 策略拒绝原因不守恒")
        final_count = set_report["final_count"]
        zero_reasons = set_report.get("executable_zero_reasons")
        if (not isinstance(zero_reasons, Mapping) or set(zero_reasons) != set(EXECUTABLE_REASONS)
                or any(type(v) is not int or not 0 <= v <= final_count for v in zero_reasons.values())):
            raise RunReportError(f"{set_name} executable 原因统计非法")
        for metric in (
            "query_usability_rate",
            "semantic_label_fit_rate",
            "stage1_admission_rate",
            "taxonomy_l1_coverage_rate",
            "taxonomy_l2_coverage_rate",
            "scope_l2_coverage_rate",
        ):
            _validate_ratio(set_report.get(metric), f"{set_name}.{metric}")
        quota_metric = set_report.get("p1_quota_fulfillment_rate")
        if set_name == "P1":
            _validate_ratio(quota_metric, "P1.p1_quota_fulfillment_rate")
        elif quota_metric is not None:
            raise RunReportError(f"{set_name} 不得包含 P1 配额完成率")
        quality_distribution = set_report.get("final_quality_distribution")
        if not isinstance(quality_distribution, Mapping) or set(quality_distribution) != {
            "intent_label", "executable"
        }:
            raise RunReportError(f"{set_name} 最终标签分布非法")
        for field, expected_keys in (
            ("intent_label", {"task", "qa"}),
            ("executable", {"0", "1"}),
        ):
            distribution = quality_distribution[field]
            if (not isinstance(distribution, Mapping)
                    or set(distribution) != set(expected_keys)
                    or any(type(value) is not int or value < 0 for value in distribution.values())
                    or sum(distribution.values()) != set_report["final_count"]):
                raise RunReportError(f"{set_name} {field} 分布不守恒")

        tree = set_report.get("semantic_tree")
        if not isinstance(tree, list) or len(tree) != report["taxonomy"]["l1_count"]:
            raise RunReportError(f"{set_name} 语义树 L1 数量非法")
        l2_rows = []
        for l1_row in tree:
            if not isinstance(l1_row, Mapping) or not isinstance(l1_row.get("l2"), list):
                raise RunReportError(f"{set_name} 语义树节点非法")
            _validate_ratio(
                l1_row.get("scope_l2_coverage_rate"),
                f"{set_name}.{l1_row.get('l1')}.scope_l2_coverage_rate",
            )
            l2_rows.extend(l1_row["l2"])
        if len(l2_rows) != report["taxonomy"]["l2_count"]:
            raise RunReportError(f"{set_name} 语义树 L2 数量非法")
        if sum(int(row["stage1_evaluated"]) for row in l2_rows) != set_report["stage1_evaluated"]:
            raise RunReportError(f"{set_name} L2 候选统计不守恒")
        if sum(int(row["final_count"]) for row in l2_rows) != set_report["final_count"]:
            raise RunReportError(f"{set_name} L2 最终统计不守恒")
        if sum(int(row["stage1_evaluated"]) for row in tree) != set_report["stage1_evaluated"]:
            raise RunReportError(f"{set_name} L1 候选统计不守恒")
        if sum(int(row["final_count"]) for row in tree) != set_report["final_count"]:
            raise RunReportError(f"{set_name} L1 最终统计不守恒")
        if sum(int(value) for value in set_report["category_fit_distribution"].values()) != set_report["stage1_evaluated"]:
            raise RunReportError(f"{set_name} category_fit 分布不守恒")
        if sum(int(row["stage1_evaluated"]) for row in set_report["waves"]) != set_report["stage1_evaluated"]:
            raise RunReportError(f"{set_name} 波次候选统计不守恒")
        if sum(int(row["final_count"]) for row in set_report["waves"]) != set_report["final_count"]:
            raise RunReportError(f"{set_name} 波次最终统计不守恒")
        total_final += int(set_report["final_count"])

    if report["totals"].get("final_records_across_sets") != total_final:
        raise RunReportError("批跑报告总记录数不守恒")


def _pct(metric: Mapping[str, Any] | None) -> str:
    if metric is None or metric.get("rate") is None:
        return "—"
    return f"{float(metric['rate']) * 100:.2f}%"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_run_report(report: Mapping[str, Any]) -> str:
    """Render the validated JSON report as a presentation-friendly Markdown file."""
    validate_run_report(report)
    lines = [
        "# Screening v4 批跑报告",
        "",
        f"> Run ID：`{_cell(report['run_id'])}`  ",
        "> 语义标签一致率由 Stage1 LLM 判断得到，是自动评估指标，不等同于人工金标准确率。",
        "",
        "## 核心汇报指标",
        "",
        "| 集合 | Stage1 候选 | Query 基础可用率 | 语义标签一致率 | Stage1 准入率 | 最终数量 | 目标范围 L2 覆盖率 | P1 配额完成率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for set_name in SET_NAMES:
        value = report["sets"][set_name]
        lines.append(
            f"| {set_name} | {value['stage1_evaluated']} | "
            f"{_pct(value['query_usability_rate'])} | "
            f"{_pct(value['semantic_label_fit_rate'])} | "
            f"{_pct(value['stage1_admission_rate'])} | "
            f"{value['final_count']} | "
            f"{_pct(value['scope_l2_coverage_rate'])} | "
            f"{_pct(value['p1_quota_fulfillment_rate'])} |"
        )

    lines.extend(
        [
            "",
            "## 指标口径",
            "",
            "- Query 基础可用率：Stage1 候选中可进入直接测试或澄清测试的比例，独立于类别核验。",
            "- 语义标签一致率：基础可用 Query 中 category_fit=exact 的比例。",
            "- Stage1 准入率：冻结筛选策略允许纳入的样本占全部 Stage1 候选的比例。",
            "- P1 的目标范围为冻结配额大于 0 的 L2；P0/P2 的目标范围为实际进入 Stage1 的 L2。",
            "- 最终集 category_fit=exact 为准入约束，不作为独立准确率结论。",
        ]
    )

    for set_name in SET_NAMES:
        value = report["sets"][set_name]
        lines.extend(
            [
                "",
                f"## {set_name} 语义树分布",
                "",
                f"- 最终数量：{value['final_count']}",
                f"- Taxonomy L1 覆盖率：{_pct(value['taxonomy_l1_coverage_rate'])}",
                f"- Taxonomy L2 覆盖率：{_pct(value['taxonomy_l2_coverage_rate'])}",
                f"- 目标范围 L2 覆盖率：{_pct(value['scope_l2_coverage_rate'])}",
            ]
        )
        if set_name == "P1":
            lines.extend(
                [
                    f"- 冻结 L2 配额完成率：{_pct(value['p1_quota_fulfillment_rate'])}",
                    (
                        "- 最终分布与配额分布偏差："
                        + (
                            "—"
                            if value["p1_target_distribution_deviation"] is None
                            else f"{value['p1_target_distribution_deviation']:.6f}"
                        )
                    ),
                ]
            )
        lines.extend(
            [
                "",
                "| L1 | Taxonomy population | L2 数 | 目标 L2 | 已覆盖目标 L2 | Stage1 候选 | 基础可用率 | 标签一致率 | Stage1 通过 | 最终数量 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for l1_row in value["semantic_tree"]:
            lines.append(
                f"| {_cell(l1_row['l1'])} | {l1_row['taxonomy_population']} | "
                f"{l1_row['taxonomy_l2_count']} | {l1_row['coverage_scope_l2_count']} | "
                f"{l1_row['covered_scope_l2_count']} | {l1_row['stage1_evaluated']} | "
                f"{_pct(l1_row['query_usability_rate'])} | "
                f"{_pct(l1_row['semantic_label_fit_rate'])} | "
                f"{l1_row['stage1_pass']} | {l1_row['final_count']} |"
            )

        relevant_l2 = [
            row
            for l1_row in value["semantic_tree"]
            for row in l1_row["l2"]
            if row["stage1_evaluated"] or row["final_count"] or row["in_coverage_scope"]
        ]
        lines.extend(
            [
                "",
                "### L2 明细",
                "",
                "| L1 | L2 | Population | 配额 | Stage1 候选 | 基础可用 | exact（可用中） | 标签一致率 | Stage1 通过 | 最终数量 | 缺口 |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in relevant_l2:
            quota = "—" if row["target_quota"] is None else row["target_quota"]
            deficit = "—" if row["deficit"] is None else row["deficit"]
            lines.append(
                f"| {_cell(row['l1'])} | {_cell(row['l2'])} | "
                f"{row['taxonomy_population']} | {quota} | {row['stage1_evaluated']} | "
                f"{row['usable_queries']} | {row['category_exact_among_usable']} | "
                f"{_pct(row['semantic_label_fit_rate'])} | {row['stage1_pass']} | "
                f"{row['final_count']} | {deficit} |"
            )

        lines.extend(
            [
                "",
                "### 波次统计",
                "",
                "| 来源 | 轮次 | Stage1 候选 | 基础可用率 | 标签一致率 | Stage1 通过 | 最终入选 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for wave in value["waves"]:
            lines.append(
                f"| {wave['origin']} | {wave['round_id']} | {wave['stage1_evaluated']} | "
                f"{_pct(wave['query_usability_rate'])} | "
                f"{_pct(wave['semantic_label_fit_rate'])} | "
                f"{wave['stage1_pass']} | {wave['final_count']} |"
            )

        category = value["category_fit_distribution"]
        lines.extend(
            [
                "",
                "### 质量摘要",
                "",
                f"- Stage1 category_fit：exact={category['exact']}，partial={category['partial']}，mismatch={category['mismatch']}。",
                f"- Stage1 五组准入结果：{json.dumps(value['stage1_admission_groups'], ensure_ascii=False, sort_keys=True)}；目标类型={json.dumps(value['stage1_intent_counts'], ensure_ascii=False, sort_keys=True)}。",
                f"- Stage1 阻断原因（多原因计数允许重叠）：{json.dumps(value['stage1_blocking_counts'], ensure_ascii=False, sort_keys=True)}；routes={json.dumps(value['stage1_route_counts'], ensure_ascii=False, sort_keys=True)}。",
                f"- intent_label：{json.dumps(value['final_quality_distribution']['intent_label'], ensure_ascii=False, sort_keys=True)}。",
                f"- executable 置零原因：{json.dumps(value['executable_zero_reasons'], ensure_ascii=False, sort_keys=True)}。",
                f"- executable：{json.dumps(value['final_quality_distribution']['executable'], ensure_ascii=False, sort_keys=True)}。",
            ]
        )

    lines.extend(
        [
            "",
            "## P1 补采诊断",
            "",
            f"- 尚有缺口的 L2：{report['p1_diagnostics']['l2_with_deficit']}；其中缺 1–2 条的 L2：{report['p1_diagnostics']['small_deficit_l2']}，合计缺口 {report['p1_diagnostics']['small_deficit_total']}。",
            "- 完整的逐批 L2 和 cluster 数据见 `P1/supplement_summary.json`。",
            "",
            "| 轮次 | 小批数 | 计划请求 | 实际抽样 | Stage1 评估 | 基础可用 | 类别 exact | 通过 | 入集 | 停止原因 |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in report["p1_diagnostics"]["rounds"]:
        lines.append(
            f"| {row['round']} | {row['batch_count']} | {row['planned_request']} | "
            f"{row['sampled']} | {row['evaluated']} | {row['usable']} | {row['exact']} | "
            f"{row['stage1_pass']} | {row['admitted']} | {row['stop_reason']} |"
        )
    lines.extend(
        [
            "",
            "### 缺口最大的 L2",
            "",
            "| L2 | 配额 | 缺口 | 补采评估 | 基础可用 | exact | 候选剩余 | 停止原因 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in report["p1_diagnostics"]["top_deficit_l2"]:
        lines.append(
            f"| {_cell(row['l2_key'])} | {row['quota']} | {row['deficit']} | "
            f"{row['evaluated']} | {row['usable']} | {row['exact']} | "
            f"{row['candidate_remaining']} | {row['stop_reason']} |"
        )
    lines.extend(
        [
            "",
            "### 损耗最多的 cluster",
            "",
            "| 原因 | L2 | cluster | Stage1 评估 | 基础可用 | exact |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for reason, key in (("文本不可用", "top_unusable_clusters"),
                        ("可用但类别不符", "top_category_mismatch_clusters")):
        for row in report["p1_diagnostics"][key]:
            lines.append(
                f"| {reason} | {_cell(row['l2_key'])} | {_cell(row['cluster_id'])} | "
                f"{row['evaluated']} | {row['usable']} | {row['exact']} |"
            )
    p2 = report["p2_diagnostics"]
    lines.extend(
        [
            "",
            "## P2 Challenge 诊断",
            "",
            f"- 最终数量：{p2['final']} / {p2['target']}；target_gap={p2['target_gap']}；停止原因={p2['stop_reason']}。",
            f"- 分类型软下限={json.dumps(p2['soft_min_by_primary'], ensure_ascii=False, sort_keys=True)}；"
            f"单一 primary 上限={p2['max_per_primary']}。",
            f"- Judge 通过但未入集：{p2['judge_pass_not_selected']}。",
            f"- 未入集原因：{json.dumps(p2['not_selected_reasons'], ensure_ascii=False, sort_keys=True)}。",
            f"- P2 批处理阶段耗时（ms）：{json.dumps(p2['phase_duration_ms'], ensure_ascii=False, sort_keys=True)}。",
            f"- P2 采样预算：{json.dumps(p2['budget'], ensure_ascii=False, sort_keys=True)}。",
            f"- 最终 frequency band：{json.dumps(p2['final_frequency_band_distribution'], ensure_ascii=False, sort_keys=True)}。",
            "",
            "### Primary Challenge 分布",
            "",
            "| 类型 | 最终数量 | 占比 | 软下限缺口 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for challenge_type in sorted(p2["soft_min_deficit_by_primary"]):
        lines.append(
            f"| {challenge_type} | {p2['final_by_primary'].get(challenge_type, 0)} | "
            f"{p2['final_share_by_primary'].get(challenge_type, 0) * 100:.2f}% | "
            f"{p2['soft_min_deficit_by_primary'][challenge_type]} |"
        )
    lines.extend(
        [
            "",
            "### 来源池漏斗",
            "",
            "| 来源 | 候选容量 | 请求 | 实际抽取 | 去重后 | Stage1 通过 | Judge 通过 | 最终入集 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for source in sorted(p2["source_funnel"]):
        row = p2["source_funnel"][source]
        lines.append(
            f"| {source} | {row['candidate_capacity']} | {row['requested']} | "
            f"{row['actual_sampled']} | {row['after_dedup']} | {row['stage1_pass']} | "
            f"{row['judge_pass']} | {row['final']} |"
        )
    runtime = p2.get("challenge_runtime", {})
    if runtime:
        lines.extend(
            [
                "",
                "### Challenge Judge 运行",
                "",
                f"- 任务数={runtime.get('task_count', 0)}，调用尝试={runtime.get('attempt_count', 0)}，缓存命中={runtime.get('cache_hits', 0)}。",
                f"- 失败={runtime.get('failure_count', 0)}，重试={runtime.get('retry_count', 0)}，平均时延={runtime.get('mean_latency_ms')} ms，P90={runtime.get('p90_latency_ms')} ms。",
            ]
        )
    lines.extend(
        [
            "",
            "## 结论使用说明",
            "",
            "- “全面性”主要看 P1 的目标范围 L2 覆盖率、冻结配额完成率和逐 L2 缺口。",
            "- P0/P2 有各自的结构和困难样本门槛，不应以全 taxonomy L2 覆盖作为硬目标。",
            "- “可用性”同时参考 Query 基础可用率、语义标签一致率和 Stage1 准入率。",
            "- 如需对外声明真实标签准确率，应在此自动报告之外增加按 L2 分层的人工抽检。",
            "",
        ]
    )
    lines.extend(["", "## 集合内候选去重", "", "| 集合 | 精确剔除 | 近似剔除 | 临界保留对 | 补位缺口 | 审计 |",
                  "| --- | ---: | ---: | ---: | ---: | --- |"])
    for name in ("P0", "P1", "P2"):
        value = report["dedup"][name]
        lines.append(f"| {name} | {value['exact_dropped']} | {value['near_dropped']} | "
                     f"{value['borderline_retained']} | {value['backfill_gap']} | "
                     f"`{value['audit_path']}` |")
    return "\n".join(lines)


def write_run_report(
    run_root: Path,
    *,
    run_id: str,
    taxonomy: TaxonomyMappings,
    evaluated_candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    evaluation_rounds: Mapping[str, Mapping[str, int]],
    stage1_results: Mapping[str, Mapping[str, Any]],
    final_records: Mapping[str, Sequence[Mapping[str, Any]]],
    p1_target_quotas: Mapping[str, int],
    build_summary: Mapping[str, Any],
) -> JsonObject:
    """Write deterministic JSON and Markdown report artifacts."""
    quality_root = run_root.expanduser().resolve() / "quality"
    tasks_path = quality_root / "challenge_tasks.jsonl"
    attempts_path = quality_root / "challenge_attempts.jsonl"
    cache_hits_path = quality_root / "challenge_cache_hits.jsonl"
    tasks = []
    attempts = []
    cache_hits = []
    if tasks_path.is_file():
        tasks = [json.loads(line) for line in tasks_path.read_text(encoding="utf-8").splitlines() if line]
    if attempts_path.is_file():
        attempts = [json.loads(line) for line in attempts_path.read_text(encoding="utf-8").splitlines() if line]
    if cache_hits_path.is_file():
        cache_hits = [json.loads(line) for line in cache_hits_path.read_text(encoding="utf-8").splitlines() if line]
    successes = [row for row in attempts if row.get("outcome") == "success"]
    latencies = sorted(float(row["full_latency_ms"]) for row in successes if row.get("full_latency_ms") is not None)
    challenge_runtime: JsonObject = {
        "task_count": len(tasks),
        "attempt_count": len(attempts),
        "cache_hits": len(cache_hits),
        "failure_count": sum(row.get("outcome") == "failure" for row in attempts),
        "failure_rate": (
            None if not attempts
            else round(sum(row.get("outcome") == "failure" for row in attempts) / len(attempts), 6)
        ),
        "retry_count": max(0, len(attempts) - len({row.get("cache_key") for row in attempts})),
        "mean_latency_ms": None if not latencies else round(statistics.fmean(latencies), 3),
        "median_latency_ms": None if not latencies else round(statistics.median(latencies), 3),
        "p90_latency_ms": None if not latencies else round(latencies[min(len(latencies) - 1, math.ceil(len(latencies) * 0.9) - 1)], 3),
        "max_latency_ms": None if not latencies else round(max(latencies), 3),
    }
    report = build_run_report(
        run_id=run_id,
        taxonomy=taxonomy,
        evaluated_candidates=evaluated_candidates,
        evaluation_rounds=evaluation_rounds,
        stage1_results=stage1_results,
        final_records=final_records,
        p1_target_quotas=p1_target_quotas,
        build_summary=build_summary,
        challenge_runtime=challenge_runtime,
    )
    run_root = run_root.expanduser().resolve()
    write_json(run_root / RUN_REPORT_JSON, report)
    (run_root / RUN_REPORT_MARKDOWN).write_text(
        render_run_report(report), encoding="utf-8", newline="\n"
    )
    return report


def load_and_validate_run_report(run_root: Path) -> JsonObject:
    """Validate both report files and their deterministic equivalence."""
    run_root = run_root.expanduser().resolve()
    try:
        report = json.loads((run_root / RUN_REPORT_JSON).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunReportError("无法读取 JSON 批跑报告") from exc
    if not isinstance(report, dict):
        raise RunReportError("JSON 批跑报告必须是对象")
    validate_run_report(report)
    expected_markdown = render_run_report(report)
    try:
        actual_markdown = (run_root / RUN_REPORT_MARKDOWN).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RunReportError("无法读取 Markdown 批跑报告") from exc
    if actual_markdown != expected_markdown:
        raise RunReportError("Markdown 批跑报告与 JSON 事实源不一致")
    return report
