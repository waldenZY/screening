"""Adaptive P2 candidate routing and challenge-aware final selection."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .index import CanonicalCandidateIndex
from .sampling import COMPLEXITY_RANK, P2SamplingConfig, allocate_sqrt_quotas
from ..quality.two_stage import CHALLENGE_TYPES, challenge_passes
from ..shared.json_io import JsonObject


P2_SOURCE_NAMES = ("complex", "multi_turn", "boundary", "exploration")
P2_ALGORITHM = "adaptive_challenge_batches_1"


def source_tags(locator: Mapping[str, Any], config: P2SamplingConfig) -> tuple[str, ...]:
    """Return all matching heuristic pools; exploration is the complement."""
    tags: list[str] = []
    if COMPLEXITY_RANK[str(locator["features"]["complexity"])] >= COMPLEXITY_RANK[config.minimum_complexity]:
        tags.append("complex")
    if locator["sample_type"] == "multi_turn":
        tags.append("multi_turn")
    if float(locator["cluster_probability"]) < config.boundary_probability_cap:
        tags.append("boundary")
    return tuple(tags or ("exploration",))


def allocate_by_weights(
    total: int,
    weights: Mapping[str, float],
    *,
    capacities: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Largest-remainder allocation with deterministic capacity redistribution."""
    if total <= 0:
        return {key: 0 for key in weights}
    caps = {key: total if capacities is None else max(0, int(capacities.get(key, 0))) for key in weights}
    target = min(total, sum(caps.values()))
    result = {key: 0 for key in weights}
    active = {key for key in weights if caps[key] > 0 and float(weights[key]) > 0}
    remaining = target
    while remaining and active:
        weight_sum = sum(float(weights[key]) for key in active)
        if weight_sum <= 0:
            break
        raw = {key: remaining * float(weights[key]) / weight_sum for key in active}
        progressed = 0
        for key in sorted(active):
            addition = min(caps[key] - result[key], int(math.floor(raw[key])))
            if addition > 0:
                result[key] += addition
                progressed += addition
        remaining -= progressed
        active = {key for key in active if result[key] < caps[key]}
        if not remaining or not active:
            break
        for key in sorted(
            active,
            key=lambda item: (-(raw.get(item, 0.0) - math.floor(raw.get(item, 0.0))), item),
        ):
            if not remaining:
                break
            result[key] += 1
            remaining -= 1
            if result[key] >= caps[key]:
                active.discard(key)
    return result


def posterior_rate(passed: int, evaluated: int, prior: float, strength: float, minimum: float) -> float:
    return max(minimum, (passed + prior * strength) / (evaluated + strength))


@dataclass(frozen=True)
class P2Draw:
    locators: tuple[JsonObject, ...]
    metadata: Mapping[str, JsonObject]
    available_by_source: Mapping[str, int]
    requested_by_source: Mapping[str, int]


class AdaptiveP2Sampler:
    """Select deterministic, stratified batches without materializing source text."""

    def __init__(self, index: CanonicalCandidateIndex, config: P2SamplingConfig):
        self.index = index
        self.config = config
        self._excluded: set[str] = set()
        self._cached_counts: dict[str, tuple[dict[str, int], dict[tuple[str, str], int]]] | None = None
        self._cached_excluded: frozenset[str] | None = None
        self.index.connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS p2_excluded (source_identity_sha256 TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        self.index.connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS p2_quotas (l2_key TEXT, cluster_json TEXT, quota INTEGER, PRIMARY KEY(l2_key, cluster_json)) WITHOUT ROWID"
        )

    def _sync_excluded(self, excluded: Iterable[str]) -> None:
        additions = sorted(set(excluded) - self._excluded)
        if additions:
            self.index.connection.executemany(
                "INSERT OR IGNORE INTO p2_excluded(source_identity_sha256) VALUES (?)",
                ((value,) for value in additions),
            )
            self._excluded.update(additions)

    def _predicate(self, source: str) -> tuple[str, tuple[Any, ...]]:
        minimum = COMPLEXITY_RANK[self.config.minimum_complexity]
        complexity_case = "CASE c.complexity WHEN 'simple' THEN 0 WHEN 'moderate' THEN 1 ELSE 2 END >= ?"
        if source == "complex":
            return complexity_case, (minimum,)
        if source == "multi_turn":
            return "c.sample_type='multi_turn'", ()
        if source == "boundary":
            return "c.cluster_probability < ?", (self.config.boundary_probability_cap,)
        if source == "exploration":
            return (
                f"NOT (({complexity_case}) OR c.sample_type='multi_turn' OR c.cluster_probability < ?)",
                (minimum, self.config.boundary_probability_cap),
            )
        raise ValueError(f"未知 P2 来源池: {source}")

    def _counts(self, source: str) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
        predicate, params = self._predicate(source)
        rows = self.index.connection.execute(
            f"""
            SELECT c.l2_key, c.cluster_json, COUNT(*)
            FROM candidates c
            LEFT JOIN p2_excluded e USING(source_identity_sha256)
            WHERE e.source_identity_sha256 IS NULL AND {predicate}
            GROUP BY c.l2_key, c.cluster_json
            ORDER BY c.l2_key, c.cluster_json
            """,
            params,
        )
        l2 = Counter()
        clusters: dict[tuple[str, str], int] = {}
        for l2_key, cluster_json, count in rows:
            l2[str(l2_key)] += int(count)
            clusters[(str(l2_key), str(cluster_json))] = int(count)
        return dict(l2), clusters

    def capacities(self, excluded: Iterable[str] = ()) -> dict[str, int]:
        self._sync_excluded(excluded)
        self._cached_counts = {source: self._counts(source) for source in P2_SOURCE_NAMES}
        self._cached_excluded = frozenset(self._excluded)
        return {source: sum(self._cached_counts[source][0].values()) for source in P2_SOURCE_NAMES}

    def _take(
        self,
        source: str,
        count: int,
        prepared: tuple[dict[str, int], dict[tuple[str, str], int]] | None = None,
    ) -> tuple[list[JsonObject], int]:
        l2_counts, cluster_counts = prepared or self._counts(source)
        available = sum(l2_counts.values())
        if count <= 0 or available == 0:
            return [], available
        l2_quotas = allocate_sqrt_quotas(l2_counts, min(count, available))
        quotas: dict[tuple[str, str], int] = {}
        for l2_key, l2_quota in l2_quotas.items():
            values = {
                cluster_json: size
                for (key, cluster_json), size in cluster_counts.items()
                if key == l2_key
            }
            for cluster_json, quota in allocate_sqrt_quotas(values, l2_quota).items():
                quotas[(l2_key, cluster_json)] = quota
        connection = self.index.connection
        connection.execute("DELETE FROM p2_quotas")
        connection.executemany(
            "INSERT INTO p2_quotas(l2_key, cluster_json, quota) VALUES (?, ?, ?)",
            ((l2, cluster, quota) for (l2, cluster), quota in sorted(quotas.items())),
        )
        predicate, params = self._predicate(source)
        rows = connection.execute(
            f"""
            WITH ranked AS (
                SELECT c.source_identity_sha256, c.conversation_sha256,
                       c.source_line, c.byte_offset, c.byte_length, c.record_sha256,
                       c.cluster_json, c.l1, c.l2, c.l2_key, c.sample_type,
                       c.cluster_probability, c.frequency, c.frequency_less_count,
                       c.cluster_frequency_count, c.probability_band,
                       c.frequency_band, c.normalized_frequency, c.relevance_p0,
                       c.relevance_p2, c.length_band, c.complexity, c.formality,
                       q.quota,
                       ROW_NUMBER() OVER (
                           PARTITION BY c.l2_key, c.cluster_json
                           ORDER BY c.source_identity_sha256
                       ) AS row_number
                FROM candidates c
                JOIN p2_quotas q ON q.l2_key=c.l2_key AND q.cluster_json=c.cluster_json
                LEFT JOIN p2_excluded e USING(source_identity_sha256)
                WHERE e.source_identity_sha256 IS NULL AND {predicate}
            )
            SELECT * FROM ranked WHERE row_number <= quota ORDER BY source_line
            """,
            params,
        ).fetchall()
        locators: list[JsonObject] = []
        for row in rows:
            locators.append({
                "source_identity_sha256": row[0],
                "conversation_sha256": row[1],
                "source_snapshot_id": self.index.handle.metadata["source_snapshot_id"],
                "source_line": int(row[2]),
                "byte_offset": int(row[3]),
                "byte_length": int(row[4]),
                "record_sha256": row[5],
                "cluster_id": json.loads(row[6]),
                "l1": row[7], "l2": row[8], "l2_key": row[9],
                "sample_type": row[10],
                "cluster_probability": float(row[11]),
                "frequency": int(row[12]),
                "cluster_frequency_stats": {
                    "less_count": int(row[13]), "total_count": int(row[14]),
                },
                "features": {
                    "probability_band": row[15], "frequency_band": row[16],
                    "normalized_frequency": float(row[17]),
                    "relevance": {"p0": float(row[18]), "p2": float(row[19])},
                    "length_band": row[20], "complexity": row[21], "formality": row[22],
                },
            })
        return locators, available

    def draw(self, requested_by_source: Mapping[str, int], excluded: Iterable[str]) -> P2Draw:
        self._sync_excluded(excluded)
        prepared = (
            self._cached_counts
            if self._cached_counts is not None and self._cached_excluded == frozenset(self._excluded)
            else None
        )
        selected: list[JsonObject] = []
        metadata: dict[str, JsonObject] = {}
        available: dict[str, int] = {}
        selected_any = False
        for source in P2_SOURCE_NAMES:
            chosen, source_available = self._take(
                source,
                max(0, int(requested_by_source.get(source, 0))),
                None if prepared is None or selected_any else prepared[source],
            )
            available[source] = source_available
            for locator in chosen:
                identity = str(locator["source_identity_sha256"])
                metadata[identity] = {
                    "source_tags": list(source_tags(locator, self.config)),
                    "sampled_from": source,
                }
            selected.extend(chosen)
            self._sync_excluded(item["source_identity_sha256"] for item in chosen)
            selected_any = selected_any or bool(chosen)
        self._cached_counts = None
        self._cached_excluded = None
        return P2Draw(
            locators=tuple(selected),
            metadata=metadata,
            available_by_source=available,
            requested_by_source={key: int(requested_by_source.get(key, 0)) for key in P2_SOURCE_NAMES},
        )


def _candidate_order(candidate: Mapping[str, Any]) -> tuple[str, int]:
    conversation = candidate["conversation"]
    text = conversation["full_text"] if conversation["sample_type"] == "multi_turn" else conversation["target_query"]
    digest = hashlib.sha256(
        (str(candidate["taxonomy"]["l2_key"]) + "\0" + text).encode("utf-8")
    ).hexdigest()
    return digest, int(candidate["source"]["source_line"])


def _diverse_take(values: Sequence[JsonObject], count: int) -> list[JsonObject]:
    """Square-root L2/cluster allocation followed by stable text-hash ordering."""
    if count <= 0:
        return []
    by_l2: dict[str, list[JsonObject]] = defaultdict(list)
    for value in values:
        by_l2[str(value["taxonomy"]["l2_key"])].append(value)
    l2_quotas = allocate_sqrt_quotas({key: len(items) for key, items in by_l2.items()}, count)
    selected: list[JsonObject] = []
    for l2_key in sorted(l2_quotas):
        by_cluster: dict[str, list[JsonObject]] = defaultdict(list)
        for value in by_l2[l2_key]:
            key = json.dumps(value["taxonomy"]["cluster_id"], ensure_ascii=False, sort_keys=True)
            by_cluster[key].append(value)
        quotas = allocate_sqrt_quotas(
            {key: len(items) for key, items in by_cluster.items()}, l2_quotas[l2_key]
        )
        for key in sorted(quotas):
            selected.extend(sorted(by_cluster[key], key=_candidate_order)[:quotas[key]])
    return selected


def select_final_p2(
    candidates: Sequence[JsonObject],
    challenge_results: Mapping[str, JsonObject],
    config: P2SamplingConfig,
) -> tuple[list[JsonObject], JsonObject]:
    """Apply eight soft floors, sqrt fill, primary cap, and diversity selection."""
    groups: dict[str, list[JsonObject]] = {key: [] for key in CHALLENGE_TYPES}
    for candidate in candidates:
        identity = str(candidate["source_identity_sha256"])
        result = challenge_results.get(identity)
        if result is not None and challenge_passes(result):
            groups[str(result["primary_challenge"])].append(candidate)

    selected: list[JsonObject] = []
    selected_ids: set[str] = set()
    counts: Counter[str] = Counter()
    for challenge_type in CHALLENGE_TYPES:
        take = min(config.soft_min_per_type, config.max_per_primary, len(groups[challenge_type]))
        values = _diverse_take(groups[challenge_type], take)
        selected.extend(values)
        selected_ids.update(str(item["source_identity_sha256"]) for item in values)
        counts[challenge_type] += len(values)

    remaining_target = max(0, config.target_n - len(selected))
    available = {
        key: max(0, min(config.max_per_primary - counts[key], len(groups[key]) - counts[key]))
        for key in CHALLENGE_TYPES
    }
    extra_quotas = allocate_sqrt_quotas(available, remaining_target, capacities=available)
    for challenge_type in CHALLENGE_TYPES:
        quota = extra_quotas.get(challenge_type, 0)
        pool = [
            item for item in groups[challenge_type]
            if str(item["source_identity_sha256"]) not in selected_ids
        ]
        values = _diverse_take(pool, quota)
        selected.extend(values)
        selected_ids.update(str(item["source_identity_sha256"]) for item in values)
        counts[challenge_type] += len(values)

    selected.sort(key=lambda item: (
        CHALLENGE_TYPES.index(str(challenge_results[str(item["source_identity_sha256"])]["primary_challenge"])),
        _candidate_order(item),
    ))
    deficits = {
        key: max(0, config.soft_min_per_type - counts[key]) for key in CHALLENGE_TYPES
    }
    type_cap_excluded = sum(
        max(0, len(groups[key]) - config.max_per_primary) for key in CHALLENGE_TYPES
    )
    capped_available = sum(
        min(len(groups[key]), config.max_per_primary) for key in CHALLENGE_TYPES
    )
    total_limit_excluded = max(0, capped_available - config.target_n)
    diagnostics: JsonObject = {
        "available_by_primary": {key: len(groups[key]) for key in CHALLENGE_TYPES},
        "selected_by_primary": {key: counts[key] for key in CHALLENGE_TYPES},
        "soft_min_deficit_by_primary": deficits,
        "target_gap": max(0, config.target_n - len(selected)),
        "judge_pass_not_selected": sum(len(values) for values in groups.values()) - len(selected),
        "not_selected_reasons": {
            "type_cap": type_cap_excluded,
            "total_limit": total_limit_excluded,
            "diversity": 0,
        },
    }
    return selected, diagnostics


def source_yield_rates(
    metadata: Mapping[str, Mapping[str, Any]],
    challenge_results: Mapping[str, Mapping[str, Any]],
    *,
    default_yield: float,
    type_default_yield: float,
    prior_strength: float,
    minimum_yield: float,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    evaluated = Counter(str(value["sampled_from"]) for value in metadata.values())
    passed = Counter()
    typed: dict[str, Counter[str]] = {source: Counter() for source in P2_SOURCE_NAMES}
    for identity, value in metadata.items():
        result = challenge_results.get(identity)
        if result and result.get("is_challenge") is True:
            source = str(value["sampled_from"])
            passed[source] += 1
            typed[source][str(result["primary_challenge"])] += 1
    total_rates = {
        source: posterior_rate(
            passed[source], evaluated[source], default_yield, prior_strength, minimum_yield
        ) for source in P2_SOURCE_NAMES
    }
    type_rates = {
        source: {
            challenge_type: posterior_rate(
                typed[source][challenge_type], evaluated[source], type_default_yield,
                prior_strength, minimum_yield,
            ) for challenge_type in CHALLENGE_TYPES
        } for source in P2_SOURCE_NAMES
    }
    return total_rates, type_rates
