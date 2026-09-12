"""Strict taxonomy contract and L1/L2 mapping loader."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .json_io import JsonObject


@dataclass(frozen=True)
class TaxonomyMappings:
    source_path: str
    cluster_to_l1: dict[Any, str]
    cluster_to_l2: dict[Any, str]
    l1_clusters: dict[str, frozenset[Any]]
    l2_clusters: dict[str, frozenset[Any]]
    l1_population: dict[str, int]
    l2_population: dict[str, int]
    l1_order: tuple[str, ...]
    declared_pct: dict[str, float]
    meta: Any
    cluster_id_type: str
    l2_size_differences: dict[str, JsonObject]


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


def _cluster_id_type(value: Any, label: str) -> str:
    if type(value) is int:
        return "integer"
    if isinstance(value, str) and value and value == value.strip():
        return "string"
    raise ValueError(f"{label} 必须是整数或无首尾空白的非空字符串")


def load_taxonomy_mappings(path: Path) -> TaxonomyMappings:
    """Load the taxonomy while treating ``_meta`` as opaque audit data."""
    data = _load_json_object(path)
    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("taxonomy.categories 必须是非空数组")
    meta = data.get("_meta", {})

    cluster_to_l1: dict[Any, str] = {}
    cluster_to_l2: dict[Any, str] = {}
    l1_clusters: dict[str, set[Any]] = defaultdict(set)
    l2_clusters: dict[str, set[Any]] = defaultdict(set)
    l1_population: dict[str, int] = {}
    l2_population: dict[str, int] = {}
    l1_order: list[str] = []
    raw_declared_pct: dict[str, float] = {}
    cluster_id_types: set[str] = set()
    l2_size_differences: dict[str, JsonObject] = {}

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
            cluster_type = _cluster_id_type(cluster_id, f"{l1}.assigned_clusters")
            if cluster_id == -1 or cluster_id == "-1":
                raise ValueError(f"{l1}.assigned_clusters 不得包含噪音 cluster -1")
            cluster_id_types.add(cluster_type)
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
                _cluster_id_type(cluster_id, f"{l2_key}.cluster_ids")
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
        if l2_size_total != l1_population[l1]:
            l2_size_differences[l1] = {
                "l1_total_size": l1_population[l1],
                "l2_size_sum": l2_size_total,
                "delta": l2_size_total - l1_population[l1],
                "all_l1_clusters_assigned_to_l2": l2_assigned == l1_clusters[l1],
            }

    if not cluster_to_l1:
        raise ValueError("taxonomy 没有可用的 assigned_clusters")
    if len(cluster_id_types) != 1:
        raise ValueError("taxonomy cluster ID 类型混用；必须全部使用整数或全部使用字符串")

    pct_scale = 1.0 if sum(raw_declared_pct.values()) <= 1.000001 else 100.0
    declared_pct = {name: value / pct_scale for name, value in raw_declared_pct.items()}
    return TaxonomyMappings(
        source_path=str(path.expanduser().resolve()),
        cluster_to_l1=cluster_to_l1,
        cluster_to_l2=cluster_to_l2,
        l1_clusters={name: frozenset(values) for name, values in l1_clusters.items()},
        l2_clusters={name: frozenset(values) for name, values in l2_clusters.items()},
        l1_population=l1_population,
        l2_population=l2_population,
        l1_order=tuple(l1_order),
        declared_pct=declared_pct,
        meta=meta,
        cluster_id_type=next(iter(cluster_id_types)),
        l2_size_differences=l2_size_differences,
    )
