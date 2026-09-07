"""Taxonomy-backed Population, quota, and deterministic candidate sampling."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .core import JsonObject, iter_jsonl, sample_identity, stable_sample_rank

L1_ONLY = "__L1_ONLY__"


@dataclass(frozen=True)
class TaxonomyMappings:
    cluster_to_l1: dict[Any, str]
    cluster_to_l2: dict[Any, str]
    l1_clusters: dict[str, frozenset[Any]]
    l2_clusters: dict[str, frozenset[Any]]
    l1_population: dict[str, int]
    l2_population: dict[str, int]
    l1_order: tuple[str, ...]
    declared_pct: dict[str, float]
    meta: JsonObject


def _load_json_object(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"taxonomy 必须是 JSON 对象: {path}")
    return value


def _non_negative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} 必须是非负整数")
    return value


def _meta_integer(meta: JsonObject, *names: str) -> int | None:
    for name in names:
        if name in meta:
            return _non_negative_integer(meta[name], f"taxonomy._meta.{name}")
    return None


def _declares_full_coverage(meta: JsonObject) -> bool:
    coverage = meta.get("coverage")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        return float(coverage) >= 1
    if isinstance(coverage, str) and "/" in coverage:
        numerator, denominator = coverage.split("/", 1)
        try:
            return int(numerator.replace(",", "")) == int(denominator.replace(",", ""))
        except ValueError:
            return False
    if isinstance(coverage, dict):
        assigned = coverage.get("assigned")
        total = coverage.get("total")
        return type(assigned) is int and type(total) is int and assigned == total
    assigned = _meta_integer(meta, "assigned", "assigned_clusters")
    cluster_total = _meta_integer(meta, "cluster_count", "total_clusters", "num_clusters")
    return assigned is not None and cluster_total is not None and assigned == cluster_total


def load_taxonomy_mappings(path: Path) -> TaxonomyMappings:
    """Load the two-level taxonomy and validate its declared aggregate values."""
    data = _load_json_object(path)
    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("taxonomy.categories 必须是非空数组")
    meta_value = data.get("_meta", {})
    if not isinstance(meta_value, dict):
        raise ValueError("taxonomy._meta 必须是对象")
    meta = dict(meta_value)

    cluster_to_l1: dict[Any, str] = {}
    cluster_to_l2: dict[Any, str] = {}
    l1_clusters: dict[str, set[Any]] = defaultdict(set)
    l2_clusters: dict[str, set[Any]] = defaultdict(set)
    l1_population: dict[str, int] = {}
    l2_population: dict[str, int] = {}
    l1_order: list[str] = []
    raw_declared_pct: dict[str, float] = {}

    for category_index, category in enumerate(categories, start=1):
        if not isinstance(category, dict):
            raise ValueError(f"taxonomy.categories[{category_index}] 必须是对象")
        l1 = str(category.get("name") or "").strip()
        if not l1 or l1 in l1_population:
            raise ValueError(f"taxonomy L1 名称为空或重复: {l1!r}")
        l1_order.append(l1)
        l1_population[l1] = _non_negative_integer(
            category.get("total_size"), f"{l1}.total_size"
        )
        pct = category.get("pct")
        if pct is not None:
            if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct < 0:
                raise ValueError(f"{l1}.pct 必须是非负数")
            raw_declared_pct[l1] = float(pct)
        assigned = category.get("assigned_clusters")
        if not isinstance(assigned, list):
            raise ValueError(f"{l1}.assigned_clusters 必须是数组")
        if len(set(map(str, assigned))) != len(assigned):
            raise ValueError(f"{l1}.assigned_clusters 存在重复 cluster")
        for cluster_id in assigned:
            previous = cluster_to_l1.get(cluster_id)
            if previous is not None and previous != l1:
                raise ValueError(
                    f"cluster {cluster_id!r} 同时映射到 L1 {previous!r} 与 {l1!r}"
                )
            cluster_to_l1[cluster_id] = l1
            l1_clusters[l1].add(cluster_id)

        sub_intents = category.get("sub_intents")
        if not isinstance(sub_intents, list):
            raise ValueError(f"{l1}.sub_intents 必须是数组")
        seen_l2: set[str] = set()
        l2_size_total = 0
        l2_assigned: set[Any] = set()
        for sub_index, sub_intent in enumerate(sub_intents, start=1):
            if not isinstance(sub_intent, dict):
                raise ValueError(f"{l1}.sub_intents[{sub_index}] 必须是对象")
            l2 = str(sub_intent.get("name") or "").strip()
            if not l2 or l2 in seen_l2:
                raise ValueError(f"{l1} 的 L2 名称为空或重复: {l2!r}")
            seen_l2.add(l2)
            l2_key = f"{l1}/{l2}"
            size = _non_negative_integer(sub_intent.get("size"), f"{l2_key}.size")
            l2_population[l2_key] = size
            l2_size_total += size
            cluster_ids = sub_intent.get("cluster_ids")
            if not isinstance(cluster_ids, list):
                raise ValueError(f"{l2_key}.cluster_ids 必须是数组")
            if len(set(map(str, cluster_ids))) != len(cluster_ids):
                raise ValueError(f"{l2_key}.cluster_ids 存在重复 cluster")
            for cluster_id in cluster_ids:
                if cluster_id not in l1_clusters[l1]:
                    raise ValueError(
                        f"cluster {cluster_id!r} 出现在 {l2_key}，但不在该 L1 assigned_clusters 中"
                    )
                previous = cluster_to_l2.get(cluster_id)
                if previous is not None and previous != l2_key:
                    raise ValueError(
                        f"cluster {cluster_id!r} 同时映射到 L2 {previous!r} 与 {l2_key!r}"
                    )
                cluster_to_l2[cluster_id] = l2_key
                l2_clusters[l2_key].add(cluster_id)
                l2_assigned.add(cluster_id)
        if l2_assigned == l1_clusters[l1] and l2_size_total != l1_population[l1]:
            raise ValueError(f"{l1} 的 L2 size 总和与 total_size 不一致")

    if not cluster_to_l1:
        raise ValueError("taxonomy 没有可用的 assigned_clusters")

    total_queries = _meta_integer(meta, "total_queries", "total_query_count", "query_count")
    if total_queries is not None and sum(l1_population.values()) != total_queries:
        raise ValueError("taxonomy L1 total_size 总和与 _meta 总 query 数不一致")
    l1_count = _meta_integer(meta, "l1_count", "num_l1")
    if l1_count is not None and l1_count != len(l1_population):
        raise ValueError("taxonomy L1 数量与 _meta 不一致")
    l2_count = _meta_integer(meta, "l2_count", "num_l2")
    if l2_count is not None and l2_count != len(l2_population):
        raise ValueError("taxonomy L2 数量与 _meta 不一致")
    cluster_count = _meta_integer(
        meta, "cluster_count", "total_clusters", "num_clusters", "assigned", "assigned_clusters"
    )
    if cluster_count is not None and cluster_count != len(cluster_to_l1):
        raise ValueError("taxonomy assigned cluster 数量与 _meta 不一致")

    pct_scale = 1.0 if sum(raw_declared_pct.values()) <= 1.000001 else 100.0
    declared_pct = {
        name: value / pct_scale for name, value in raw_declared_pct.items()
    }

    return TaxonomyMappings(
        cluster_to_l1=cluster_to_l1,
        cluster_to_l2=cluster_to_l2,
        l1_clusters={name: frozenset(values) for name, values in l1_clusters.items()},
        l2_clusters={name: frozenset(values) for name, values in l2_clusters.items()},
        l1_population=l1_population,
        l2_population=l2_population,
        l1_order=tuple(l1_order),
        declared_pct=declared_pct,
        meta=meta,
    )


def build_population(
    upstream_path: Path,
    mappings: TaxonomyMappings,
    *,
    deduplicate: bool = True,
) -> JsonObject:
    """Use taxonomy sizes as Population and scan cluster data for diagnostics."""
    cluster_counts: Counter[str] = Counter()
    unmapped_clusters: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    observed_clusters: set[Any] = set()
    raw_unmapped_clusters: set[Any] = set()
    total_rows = duplicate_rows = mapped_rows = unmapped_rows = noise_rows = 0
    frequency_sum = mapped_frequency_sum = 0.0
    invalid_frequency_rows = 0

    for _, row in iter_jsonl(upstream_path):
        total_rows += 1
        cluster_id = row.get("cluster")
        if cluster_id != -1 and str(cluster_id) != "-1":
            observed_clusters.add(cluster_id)
            if cluster_id not in mappings.cluster_to_l1:
                raw_unmapped_clusters.add(cluster_id)
        frequency = row.get("frequency")
        if frequency is not None:
            if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or frequency < 0:
                invalid_frequency_rows += 1
            else:
                frequency_sum += float(frequency)
        identity = sample_identity(row)
        if deduplicate and identity in seen:
            duplicate_rows += 1
            continue
        seen.add(identity)
        if cluster_id == -1 or str(cluster_id) == "-1":
            noise_rows += 1
            continue
        l1 = mappings.cluster_to_l1.get(cluster_id)
        if l1 is None:
            unmapped_rows += 1
            unmapped_clusters[str(cluster_id)] += 1
            continue
        mapped_rows += 1
        cluster_counts[str(cluster_id)] += 1
        if isinstance(frequency, (int, float)) and not isinstance(frequency, bool) and frequency >= 0:
            mapped_frequency_sum += float(frequency)

    missing_clusters = sorted(
        (str(cluster) for cluster in set(mappings.cluster_to_l1) - observed_clusters)
    )
    if _declares_full_coverage(mappings.meta):
        if raw_unmapped_clusters:
            raise ValueError("taxonomy 声明完整覆盖，但 cluster 全集存在非噪音未映射 cluster")
        if missing_clusters:
            raise ValueError("taxonomy 声明完整覆盖，但部分 assigned cluster 在全集中不存在")

    taxonomy_total = sum(mappings.l1_population.values())
    unique_rows = total_rows - duplicate_rows
    pct_by_l1 = {
        name: count / taxonomy_total if taxonomy_total else 0.0
        for name, count in mappings.l1_population.items()
    }
    pct_differences = {
        name: {
            "declared": mappings.declared_pct[name],
            "calculated": pct_by_l1[name],
            "delta": mappings.declared_pct[name] - pct_by_l1[name],
        }
        for name in mappings.declared_pct
        if abs(mappings.declared_pct[name] - pct_by_l1[name]) > 1e-6
    }
    return {
        "count_mode": "taxonomy_total_size",
        "deduplicate_candidates": deduplicate,
        "taxonomy_meta": mappings.meta,
        "taxonomy_total": taxonomy_total,
        "total_rows": total_rows,
        "unique_rows": unique_rows,
        "row_count_vs_taxonomy_delta": total_rows - taxonomy_total,
        "duplicate_rows": duplicate_rows,
        "mapped_rows": mapped_rows,
        "unmapped_rows": unmapped_rows,
        "noise_rows": noise_rows,
        "unmapped_clusters": dict(sorted(unmapped_clusters.items())),
        "missing_taxonomy_clusters": missing_clusters,
        "frequency_sum": frequency_sum,
        "mapped_frequency_sum": mapped_frequency_sum,
        "frequency_vs_row_count_delta": frequency_sum - total_rows,
        "frequency_vs_taxonomy_delta": frequency_sum - taxonomy_total,
        "invalid_frequency_rows": invalid_frequency_rows,
        "cluster_prob_used": False,
        "frequency_used": False,
        "l1_counts": dict(mappings.l1_population),
        "l1_pct": pct_by_l1,
        "pct_differences": pct_differences,
        "l2_counts": dict(mappings.l2_population),
        "cluster_counts": dict(sorted(cluster_counts.items())),
    }


def allocate_largest_remainder(
    population_by_l1: dict[str, int],
    target_n: int,
    alpha: float,
) -> tuple[dict[str, int], dict[str, float]]:
    """Allocate exact integer L1 quotas in taxonomy category order."""
    if target_n < 0:
        raise ValueError("target_n 不能小于 0")
    if not isinstance(alpha, (int, float)) or alpha < 0:
        raise ValueError("alpha 不能小于 0")
    names = list(population_by_l1)
    positive = {name: int(population_by_l1[name]) for name in names if int(population_by_l1[name]) > 0}
    weights = {name: count ** float(alpha) for name, count in positive.items()}
    total_weight = sum(weights.values())
    if target_n and not total_weight:
        raise ValueError("映射后的 Population 为空，无法分配 P1 配额")
    shares = {name: weights.get(name, 0.0) / total_weight if total_weight else 0.0 for name in names}
    exact = {name: target_n * shares[name] for name in positive}
    quotas = {name: math.floor(exact.get(name, 0.0)) for name in names}
    remainder = target_n - sum(quotas.values())
    order_index = {name: index for index, name in enumerate(names)}
    remainder_order = sorted(
        positive,
        key=lambda name: (-(exact[name] - quotas[name]), order_index[name]),
    )
    for name in remainder_order[:remainder]:
        quotas[name] += 1
    return quotas, shares


def load_upstream_pool(
    upstream_path: Path,
    mappings: TaxonomyMappings,
    *,
    excluded_identities: set[tuple[str, str]] | None = None,
    deduplicate: bool = True,
) -> tuple[dict[str, list[JsonObject]], JsonObject]:
    """Load mapped, unused, unique upstream records grouped by L1."""
    excluded = excluded_identities or set()
    seen: set[tuple[str, str]] = set()
    pools: dict[str, list[JsonObject]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for source_line, row in iter_jsonl(upstream_path):
        stats["total_rows"] += 1
        identity = sample_identity(row)
        if deduplicate and identity in seen:
            stats["duplicate_rows"] += 1
            continue
        seen.add(identity)
        cluster_id = row.get("cluster")
        if cluster_id == -1 or str(cluster_id) == "-1":
            stats["noise_rows"] += 1
            continue
        l1 = mappings.cluster_to_l1.get(cluster_id)
        if l1 is None:
            stats["unmapped_rows"] += 1
            continue
        if identity in excluded:
            stats["excluded_rows"] += 1
            continue
        annotated = dict(row)
        annotated["_target_level"] = "l1"
        annotated["_target_category"] = l1
        pools[l1].append(
            {
                "source_line": source_line,
                "L1": l1,
                "L2": mappings.cluster_to_l2.get(cluster_id, L1_ONLY),
                "cluster": cluster_id,
                "identity": list(identity),
                "record": annotated,
            }
        )
        stats["available_rows"] += 1
    return dict(pools), dict(stats)


def stable_take(
    records: Iterable[JsonObject],
    count: int,
    *,
    seed: int,
    namespace: str,
) -> list[JsonObject]:
    """Take a deterministic pseudo-random subset without input-order dependence."""
    if count <= 0:
        return []
    ranked = sorted(
        records,
        key=lambda item: (
            stable_sample_rank(seed, namespace, item.get("record", item)),
            int(item.get("source_line", 0)),
        ),
    )
    return ranked[:count]
