"""Deterministic P0/P1/P2 sampling over the canonical index.

The implementation intentionally has no dependency on either LLM stage.  It
materializes source text only when the legacy MMR/Jaccard rules require it and
returns canonical locators to the orchestration layer.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .index import CanonicalCandidateIndex
from ..shared.json_io import JsonObject
from ..shared.taxonomy import TaxonomyMappings


COMPLEXITY_RANK = {"simple": 0, "moderate": 1, "complex": 2}
PROBABILITY_ORDER = ("high", "mid", "boundary")
FREQUENCY_ORDER = ("hot", "warm", "cold")


@dataclass(frozen=True)
class P0SamplingConfig:
    l1_big_threshold: int = 3000
    l2_per_l1_big: int = 3
    l2_per_l1_small: int = 2
    max_per_cluster: int = 5
    fallback_pool_size: int = 10
    mmr_lambda: float = 0.7
    dedup_threshold: float = 0.7

    def __post_init__(self) -> None:
        for name in (
            "l1_big_threshold", "l2_per_l1_big", "l2_per_l1_small",
            "max_per_cluster", "fallback_pool_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"P0 {name} 必须是正整数")
        if not 0 <= self.mmr_lambda <= 1 or not 0 <= self.dedup_threshold <= 1:
            raise ValueError("P0 MMR/Jaccard 参数必须位于 [0, 1]")


@dataclass(frozen=True)
class P1SamplingConfig:
    target_n: int = 10_000
    probability_weights: Mapping[str, int] = field(
        default_factory=lambda: {"high": 3, "mid": 2, "boundary": 1}
    )
    frequency_weights: Mapping[str, int] = field(
        default_factory=lambda: {"hot": 2, "warm": 2, "cold": 1}
    )
    cold_boundary_floor: int = 1
    mmr_lambda: float = 0.7
    dedup_threshold: float = 0.75

    def __post_init__(self) -> None:
        if isinstance(self.target_n, bool) or not isinstance(self.target_n, int) or self.target_n < 1:
            raise ValueError("P1 target_n 必须是正整数")
        if set(self.probability_weights) != set(PROBABILITY_ORDER):
            raise ValueError("P1 probability_weights 字段不完整")
        if set(self.frequency_weights) != set(FREQUENCY_ORDER):
            raise ValueError("P1 frequency_weights 字段不完整")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in self.probability_weights.values()):
            raise ValueError("P1 probability_weights 必须是非负整数")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in self.frequency_weights.values()):
            raise ValueError("P1 frequency_weights 必须是非负整数")
        if not any(self.probability_weights.values()) or not any(self.frequency_weights.values()):
            raise ValueError("P1 九宫格权重不能全为零")
        if isinstance(self.cold_boundary_floor, bool) or not isinstance(self.cold_boundary_floor, int) or self.cold_boundary_floor < 0:
            raise ValueError("P1 cold_boundary_floor 必须是非负整数")
        if not 0 <= self.mmr_lambda <= 1 or not 0 <= self.dedup_threshold <= 1:
            raise ValueError("P1 MMR/Jaccard 参数必须位于 [0, 1]")


@dataclass(frozen=True)
class P2SamplingConfig:
    target_n: int = 2000
    candidate_source_shares: Mapping[str, float] = field(default_factory=lambda: {
        "complex": 0.30,
        "multi_turn": 0.30,
        "boundary": 0.20,
        "exploration": 0.20,
    })
    minimum_complexity: str = "moderate"
    boundary_probability_cap: float = 0.5
    soft_min_per_type: int = 100
    max_per_primary: int = 700
    batch_size: int = 2000
    dedup_threshold: float = 0.75

    def __post_init__(self) -> None:
        if isinstance(self.target_n, bool) or not isinstance(self.target_n, int) or self.target_n < 1:
            raise ValueError("P2 target_n 必须是正整数")
        expected_sources = {"complex", "multi_turn", "boundary", "exploration"}
        if set(self.candidate_source_shares) != expected_sources:
            raise ValueError("P2 candidate_source_shares 字段不完整")
        shares = list(self.candidate_source_shares.values())
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 for value in shares):
            raise ValueError("P2 candidate_source_shares 必须是非负数")
        if not math.isclose(sum(shares), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("P2 candidate_source_shares 之和必须为 1")
        if (
            isinstance(self.boundary_probability_cap, bool)
            or not isinstance(self.boundary_probability_cap, (int, float))
            or not 0 < self.boundary_probability_cap <= 1
        ):
            raise ValueError("P2 boundary_probability_cap 必须位于 (0, 1]")
        for name in ("max_per_primary", "batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"P2 {name} 必须是正整数")
        if isinstance(self.soft_min_per_type, bool) or not isinstance(self.soft_min_per_type, int) or self.soft_min_per_type < 0:
            raise ValueError("P2 soft_min_per_type 必须是非负整数")
        if self.soft_min_per_type * 8 > self.target_n:
            raise ValueError("P2 八类软下限之和不能超过 target_n")
        if self.max_per_primary < self.soft_min_per_type:
            raise ValueError("P2 max_per_primary 不能小于软下限")
        if self.max_per_primary * 8 < self.target_n:
            raise ValueError("P2 八类 primary 上限之和不能小于 target_n")
        if (
            isinstance(self.dedup_threshold, bool)
            or not isinstance(self.dedup_threshold, (int, float))
            or not 0 <= self.dedup_threshold <= 1
        ):
            raise ValueError("P2 dedup_threshold 必须位于 [0, 1]")
        if self.minimum_complexity not in COMPLEXITY_RANK:
            raise ValueError("P2 minimum_complexity 枚举非法")


@dataclass(frozen=True)
class SamplingConfig:
    p0: P0SamplingConfig = field(default_factory=P0SamplingConfig)
    p1: P1SamplingConfig = field(default_factory=P1SamplingConfig)
    p2: P2SamplingConfig = field(default_factory=P2SamplingConfig)


def character_bigrams(text: str) -> frozenset[str]:
    return frozenset(text[index : index + 2] for index in range(max(0, len(text) - 1)))


def jaccard_similarity(left: str, right: str) -> float:
    left_set = character_bigrams(left)
    right_set = character_bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def allocate_sqrt_quotas(
    sizes: Mapping[str, int],
    target_n: int,
    *,
    capacities: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Allocate an exactly conserved target with sqrt weights and hard capacities."""
    if isinstance(target_n, bool) or not isinstance(target_n, int) or target_n < 0:
        raise ValueError("target_n 必须是非负整数")
    usable: dict[str, int] = {}
    for key, size in sizes.items():
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"population 非法: {key}")
        capacity = size if capacities is None else capacities.get(key, 0)
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError(f"capacity 非法: {key}")
        if capacity > 0:
            usable[key] = capacity
    target = min(target_n, sum(usable.values()))
    if target == 0:
        return {}
    quotas = {key: 0 for key in usable}
    order = sorted(usable, key=lambda key: (-sizes.get(key, 0), key))
    covered = order if target >= len(order) else order[:target]
    for key in covered:
        quotas[key] = 1
    remaining = target - len(covered)
    weights = {key: math.sqrt(max(sizes.get(key, 0), 0)) for key in usable}
    while remaining:
        candidates = [key for key in usable if quotas[key] < usable[key]]
        if not candidates:
            break
        best = max(
            candidates,
            key=lambda key: (weights[key] / (quotas[key] + 1), -order.index(key)),
        )
        quotas[best] += 1
        remaining -= 1
    return {key: value for key, value in quotas.items() if value}


def allocate_feedback_cluster_quotas(
    remaining: Mapping[Any, int],
    target: int,
    *,
    evaluated_passed: Mapping[Any, tuple[int, int]],
    l2_prior_rate: float,
    prior_strength: float,
    round_draws: Mapping[Any, int],
    explore_all_clusters: bool,
) -> dict[Any, int]:
    """Split one P1 supplement batch using remaining size and Stage1 yield."""
    sizes = {key: count for key, count in remaining.items() if count > 0}
    target = min(target, sum(sizes.values()))
    if target <= 0:
        return {}
    quotas = {key: 0 for key in sizes}
    order = sorted(sizes, key=lambda key: (-sizes[key], str(key)))
    # Preserve V2's one-per-cluster exploration once per outer round.
    if explore_all_clusters:
        for key in order:
            if target == 0:
                break
            if round_draws.get(key, 0) == 0:
                quotas[key] += 1
                target -= 1
    weights = {}
    for key, count in sizes.items():
        evaluated, passed = evaluated_passed.get(key, (0, 0))
        posterior = (passed + prior_strength * l2_prior_rate) / (evaluated + prior_strength)
        weights[key] = math.sqrt(count) * posterior
    while target:
        eligible = [key for key in sizes if quotas[key] < sizes[key]]
        if not eligible:
            break
        chosen = min(
            eligible,
            key=lambda key: (
                -weights[key] / (round_draws.get(key, 0) + quotas[key] + 1),
                str(key),
            ),
        )
        quotas[chosen] += 1
        target -= 1
    return {key: value for key, value in quotas.items() if value}


def _weighted_grid_quotas(
    counts: Mapping[tuple[str, str], int],
    target: int,
    config: P1SamplingConfig,
) -> dict[tuple[str, str], int]:
    weighted_total = sum(
        config.probability_weights[pb] * config.frequency_weights[fb] * count
        for (pb, fb), count in counts.items()
    )
    if target <= 0 or weighted_total <= 0:
        return {}
    quotas: dict[tuple[str, str], int] = {}
    for pb in PROBABILITY_ORDER:
        for fb in FREQUENCY_ORDER:
            key = (pb, fb)
            count = counts.get(key, 0)
            if not count:
                continue
            weight = config.probability_weights[pb] * config.frequency_weights[fb]
            quotas[key] = min(count, max(0, round(target * weight * count / weighted_total)))
    cold_boundary = ("boundary", "cold")
    if counts.get(cold_boundary, 0):
        quotas[cold_boundary] = min(
            counts[cold_boundary],
            max(quotas.get(cold_boundary, 0), config.cold_boundary_floor),
        )
    while sum(quotas.values()) > target:
        reducible = [
            key for key, value in quotas.items()
            if value > (config.cold_boundary_floor if key == cold_boundary else 0)
        ]
        if not reducible:
            break
        chosen = min(reducible, key=lambda key: (quotas[key], key))
        quotas[chosen] -= 1
    return {key: value for key, value in quotas.items() if value}


class CandidateSampler:
    """Legacy-compatible branch sampler backed by :class:`CanonicalCandidateIndex`."""

    def __init__(
        self,
        index: CanonicalCandidateIndex,
        source_path: Path,
        taxonomy: TaxonomyMappings,
        config: SamplingConfig | None = None,
    ) -> None:
        self.index = index
        self.source_path = source_path
        self.taxonomy = taxonomy
        self.config = config or SamplingConfig()
        self._canonical_cache: dict[str, JsonObject] = {}

    def _materialize(self, locator: Mapping[str, Any]) -> JsonObject:
        identity = str(locator["source_identity_sha256"])
        if identity not in self._canonical_cache:
            self._canonical_cache[identity] = self.index.materialize(
                self.source_path, locator, self.taxonomy
            )
        return self._canonical_cache[identity]

    def _text(self, locator: Mapping[str, Any]) -> str:
        candidate = self._materialize(locator)
        conversation = candidate["conversation"]
        return (
            conversation["full_text"]
            if conversation["sample_type"] == "multi_turn"
            else conversation["target_query"]
        )

    def _text_uncached(self, locator: Mapping[str, Any]) -> str:
        candidate = self.index.materialize(self.source_path, locator, self.taxonomy)
        conversation = candidate["conversation"]
        return (
            conversation["full_text"]
            if conversation["sample_type"] == "multi_turn"
            else conversation["target_query"]
        )

    def _unique_text(self, values: Iterable[JsonObject]) -> list[JsonObject]:
        result: list[JsonObject] = []
        seen: set[str] = set()
        for value in values:
            text = self._text(value)
            if text not in seen:
                seen.add(text)
                result.append(value)
        return result

    def _mmr(
        self,
        values: Sequence[JsonObject],
        count: int,
        score: str,
        lam: float,
    ) -> list[JsonObject]:
        if count <= 0:
            return []
        if len(values) <= count:
            return list(values)
        ranked = sorted(
            values,
            key=lambda value: (-self._score(value, score), int(value["source_line"])),
        )
        selected = [ranked[0]]
        remaining = ranked[1:]
        while len(selected) < count and remaining:
            best = max(
                remaining,
                key=lambda value: (
                    lam * self._score(value, score)
                    + (1.0 - lam)
                    * (1.0 - max(jaccard_similarity(self._text(value), self._text(item)) for item in selected)),
                    -int(value["source_line"]),
                ),
            )
            selected.append(best)
            remaining.remove(best)
        return selected

    @staticmethod
    def _score(value: Mapping[str, Any], score: str) -> float:
        if score == "cluster_probability":
            return float(value["cluster_probability"])
        return float(value["features"]["relevance"][score])

    def sample_p0(self) -> list[JsonObject]:
        config = self.config.p0
        cluster_counts = self.index.available_counts("cluster")
        result: list[JsonObject] = []
        for l1 in self.taxonomy.l1_order:
            l2_keys = [key for key in self.taxonomy.l2_population if key.startswith(f"{l1}/")]
            l2_keys.sort(key=lambda key: (-self.taxonomy.l2_population[key], key))
            take_l2 = (
                config.l2_per_l1_big
                if self.taxonomy.l1_population[l1] >= config.l1_big_threshold
                else config.l2_per_l1_small
            )
            for l2_key in l2_keys[:take_l2]:
                clusters = sorted(
                    self.taxonomy.l2_clusters.get(l2_key, ()),
                    key=lambda cluster: (-cluster_counts.get(cluster, 0), str(cluster)),
                )
                if not clusters or cluster_counts.get(clusters[0], 0) == 0:
                    continue
                pool = self._unique_text(self.index.iter_by("cluster", clusters[0]))
                high = [item for item in pool if item["features"]["probability_band"] == "high"]
                if not high:
                    high = sorted(
                        pool,
                        key=lambda item: (-self._score(item, "p0"), int(item["source_line"])),
                    )[: config.fallback_pool_size]
                result.extend(self._mmr(high, config.max_per_cluster, "p0", config.mmr_lambda))
                self._canonical_cache.clear()
        return result

    def sample_p1(
        self,
        target_n: int | None = None,
        excluded: Iterable[str] = (),
        *,
        requested_by_l2: Mapping[str, int] | None = None,
    ) -> list[JsonObject]:
        config = self.config.p1
        target = config.target_n if target_n is None else target_n
        excluded_set = set(excluded)
        capacities: dict[str, int] = {}
        for l2_key in self.taxonomy.l2_population:
            count = sum(
                1 for item in self.index.iter_by("l2", l2_key)
                if item["source_identity_sha256"] not in excluded_set
            )
            if count:
                capacities[l2_key] = count
        if requested_by_l2 is None:
            l2_quotas = allocate_sqrt_quotas(
                self.taxonomy.l2_population,
                target,
                capacities=capacities,
            )
        else:
            l2_quotas = {
                key: min(int(value), capacities.get(key, 0))
                for key, value in requested_by_l2.items()
                if value > 0 and capacities.get(key, 0)
            }
        result: list[JsonObject] = []
        for l2_key, quota in l2_quotas.items():
            cluster_sizes: dict[str, int] = {}
            cluster_lookup: dict[str, Any] = {}
            for cluster_id in self.taxonomy.l2_clusters.get(l2_key, ()):
                count = sum(
                    1 for item in self.index.iter_by("cluster", cluster_id)
                    if item["source_identity_sha256"] not in excluded_set
                )
                if count:
                    cluster_sizes[str(cluster_id)] = count
                    cluster_lookup[str(cluster_id)] = cluster_id
            cluster_quotas = allocate_sqrt_quotas(cluster_sizes, quota)
            for cluster_key, cluster_quota in cluster_quotas.items():
                pool = self._unique_text(
                    item for item in self.index.iter_by("cluster", cluster_lookup[cluster_key])
                    if item["source_identity_sha256"] not in excluded_set
                )
                cells: dict[tuple[str, str], list[JsonObject]] = defaultdict(list)
                for item in pool:
                    cells[(
                        item["features"]["probability_band"],
                        item["features"]["frequency_band"],
                    )].append(item)
                grid = _weighted_grid_quotas(
                    {key: len(values) for key, values in cells.items()},
                    min(cluster_quota, len(pool)),
                    config,
                )
                chosen: list[JsonObject] = []
                for pb in PROBABILITY_ORDER:
                    for fb in FREQUENCY_ORDER:
                        key = (pb, fb)
                        chosen.extend(self._mmr(cells.get(key, ()), grid.get(key, 0), "p0", config.mmr_lambda))
                if len(chosen) < min(cluster_quota, len(pool)):
                    chosen_ids = {item["source_identity_sha256"] for item in chosen}
                    remaining = [item for item in pool if item["source_identity_sha256"] not in chosen_ids]
                    remaining.sort(key=lambda item: (-self._score(item, "p0"), int(item["source_line"])))
                    chosen.extend(remaining[: min(cluster_quota, len(pool)) - len(chosen)])
                result.extend(chosen)
                self._canonical_cache.clear()
        return result

    def sample_p1_supplement_batch(
        self,
        requested_by_l2: Mapping[str, int],
        excluded: set[str],
        *,
        remaining_by_cluster: Mapping[Any, int],
        evaluated_passed_by_cluster: Mapping[Any, tuple[int, int]],
        prior_by_l2: Mapping[str, float],
        prior_strength: float,
        round_draws_by_cluster: Mapping[Any, int],
        round_cold_by_cluster: Mapping[Any, int],
        round_request_by_l2: Mapping[str, int],
        round_cluster_count_by_l2: Mapping[str, int],
    ) -> list[JsonObject]:
        """Select one adaptive P1 batch while keeping V2's within-cluster rules."""
        config = self.config.p1
        result: list[JsonObject] = []
        for l2_key in sorted(requested_by_l2):
            target = requested_by_l2[l2_key]
            if target <= 0:
                continue
            clusters = self.taxonomy.l2_clusters.get(l2_key, ())
            sizes = {
                cluster: int(remaining_by_cluster.get(cluster, 0))
                for cluster in clusters
                if remaining_by_cluster.get(cluster, 0) > 0
            }
            quotas = allocate_feedback_cluster_quotas(
                sizes,
                target,
                evaluated_passed=evaluated_passed_by_cluster,
                l2_prior_rate=prior_by_l2[l2_key],
                prior_strength=prior_strength,
                round_draws=round_draws_by_cluster,
                explore_all_clusters=(
                    round_request_by_l2[l2_key] >= round_cluster_count_by_l2[l2_key]
                ),
            )
            for cluster in sorted(quotas, key=str):
                quota = quotas[cluster]
                pool = self._unique_text(
                    item for item in self.index.iter_by("cluster", cluster)
                    if item["source_identity_sha256"] not in excluded
                )
                cells: dict[tuple[str, str], list[JsonObject]] = defaultdict(list)
                for item in pool:
                    cells[(
                        item["features"]["probability_band"],
                        item["features"]["frequency_band"],
                    )].append(item)
                remaining_floor = max(
                    0, config.cold_boundary_floor - round_cold_by_cluster.get(cluster, 0)
                )
                grid = _weighted_grid_quotas(
                    {key: len(values) for key, values in cells.items()},
                    min(quota, len(pool)),
                    replace(config, cold_boundary_floor=min(quota, remaining_floor)),
                )
                chosen: list[JsonObject] = []
                for pb in PROBABILITY_ORDER:
                    for fb in FREQUENCY_ORDER:
                        key = (pb, fb)
                        chosen.extend(self._mmr(cells.get(key, ()), grid.get(key, 0), "p0", config.mmr_lambda))
                if len(chosen) < min(quota, len(pool)):
                    chosen_ids = {item["source_identity_sha256"] for item in chosen}
                    rest = [item for item in pool if item["source_identity_sha256"] not in chosen_ids]
                    rest.sort(key=lambda item: (-self._score(item, "p0"), int(item["source_line"])))
                    chosen.extend(rest[:min(quota, len(pool)) - len(chosen)])
                result.extend(chosen)
                self._canonical_cache.clear()
        return result

def deduplicate_branch_candidates(
    set_name: str,
    candidates: Sequence[JsonObject],
    existing: Sequence[JsonObject] = (),
    *,
    text_getter: Any | None = None,
    threshold: float | None = None,
) -> list[JsonObject]:
    """Deduplicate one branch while keeping ``existing`` records immutable."""
    if set_name not in {"P0", "P1", "P2"}:
        raise ValueError(f"未知数据集: {set_name}")
    limits = {"P0": 0.7, "P1": 0.75, "P2": 0.75}
    limit = limits[set_name] if threshold is None else threshold
    if not 0.0 <= limit <= 1.0:
        raise ValueError("Jaccard 阈值必须位于 [0, 1]")
    getter = text_getter or (
        lambda item: (
            item["conversation"]["full_text"]
            if "conversation" in item
            else item["selection_text"]
        )
    )
    score_name = "p2" if set_name == "P2" else "cluster_probability"

    def score(item: Mapping[str, Any]) -> float:
        if score_name == "cluster_probability":
            if "signals" in item:
                return float(item["signals"]["cluster_probability"])
            return float(item["cluster_probability"])
        return float(item["features"]["relevance"]["p2"])

    accepted = list(existing)
    accepted_bigrams = [character_bigrams(str(getter(item))) for item in accepted]
    inverted: dict[str, set[int]] = defaultdict(set)
    for index, grams in enumerate(accepted_bigrams):
        for gram in grams:
            inverted[gram].add(index)
    existing_ids = {item["source_identity_sha256"] for item in existing}
    ranked = sorted(
        (item for item in candidates if item["source_identity_sha256"] not in existing_ids),
        key=lambda item: (-score(item), int(item.get("source_line", item.get("source", {}).get("source_line", 0)))),
    )
    newly_accepted: list[JsonObject] = []
    for item in ranked:
        grams = character_bigrams(str(getter(item)))
        possible: set[int] = set()
        for gram in grams:
            possible.update(inverted.get(gram, ()))
        duplicate = False
        for index in possible:
            other = accepted_bigrams[index]
            similarity = len(grams & other) / len(grams | other) if grams and other else 0.0
            if similarity > limit:
                duplicate = True
                break
        if duplicate:
            continue
        index = len(accepted)
        accepted.append(item)
        accepted_bigrams.append(grams)
        newly_accepted.append(item)
        for gram in grams:
            inverted[gram].add(index)
    return newly_accepted
