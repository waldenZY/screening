"""Diagnostic CLI backed by the pipeline's taxonomy and sampling implementation."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from pathlib import Path

from .cluster_index import (
    CandidateIndex,
    ensure_cluster_index,
    identity_sha256,
    read_indexed_record,
)
from .core import (
    JsonObject,
    iter_jsonl,
    non_negative_int,
    sha256_file,
    write_json,
    write_jsonl,
)
from .coverage import (
    allocate_largest_remainder,
    load_taxonomy_mappings,
)
from .logging_config import setup_logger


def distribution_snapshot(
    taxonomy_path: Path,
    upstream_path: Path,
    *,
    level: str = "l1",
    alpha: float = 0.5,
    target_n: int | None = None,
    deduplicate: bool = True,
    cache_root: Path | None = None,
    seed: int = 42,
) -> JsonObject:
    if not deduplicate:
        raise ValueError("索引化链路固定按 session/session_id + text 去重")
    mappings = load_taxonomy_mappings(taxonomy_path)
    handle = ensure_cluster_index(
        upstream_path,
        mappings,
        taxonomy_sha256=sha256_file(taxonomy_path),
        cache_root=cache_root or (Path(__file__).resolve().parent / "outputs" / "_cluster_cache"),
        seed=seed,
    )
    population = handle.metadata["population"]
    counts = population[f"{level}_counts"]
    weights = {name: int(count) ** alpha for name, count in counts.items() if int(count) > 0}
    total = sum(weights.values())
    shares = {name: value / total for name, value in sorted(weights.items())} if total else {}
    output: JsonObject = {
        "schema_version": 2,
        "level": level,
        "alpha": alpha,
        "population": population,
        "target_share": shares,
    }
    if target_n is not None:
        if level != "l1":
            raise ValueError("第一版只支持计算 L1 硬配额")
        quotas, shares = allocate_largest_remainder(counts, target_n, alpha)
        output["target_n"] = target_n
        output["target_share"] = shares
        output["quota"] = quotas
    return output


def stream_candidates(
    taxonomy_path: Path,
    upstream_path: Path,
    *,
    level: str,
    targets: list[str],
    per_category: int,
    exclude_paths: list[Path],
    seed: int,
    deduplicate: bool = True,
    cache_root: Path | None = None,
) -> tuple[Iterator[JsonObject], JsonObject]:
    if not deduplicate:
        raise ValueError("索引化链路固定按 session/session_id + text 去重")
    mappings = load_taxonomy_mappings(taxonomy_path)
    known = mappings.l1_clusters if level == "l1" else mappings.l2_clusters
    unknown = sorted(set(targets) - set(known))
    valid = list(dict.fromkeys(target for target in targets if target in known))
    if not valid:
        raise ValueError("所有目标类别均不在 taxonomy 中")
    excluded: set[str] = set()
    for path in exclude_paths:
        excluded.update(identity_sha256(row) for _, row in iter_jsonl(path))
    handle = ensure_cluster_index(
        upstream_path,
        mappings,
        taxonomy_sha256=sha256_file(taxonomy_path),
        cache_root=cache_root or (Path(__file__).resolve().parent / "outputs" / "_cluster_cache"),
        seed=seed,
    )
    counts: JsonObject = {}
    summary: JsonObject = {
        "level": level,
        "unknown_targets": unknown,
        "categories": counts,
        "pool": {
            "indexed_candidates": handle.metadata["population"]["mapped_rows"],
            "excluded_identity_hashes": len(excluded),
            "selection_mode": "sqlite_taxonomy_stable_rank",
        },
    }

    def records() -> Iterator[JsonObject]:
        with CandidateIndex(handle) as candidates:
            candidates.exclude(excluded)
            for target in valid:
                available = candidates.available_count(level, target)
                limit = None if per_category == 0 else per_category
                extracted = 0
                for candidate in candidates.iter_by(level, target, limit=limit):
                    record = dict(read_indexed_record(upstream_path, candidate))
                    record["_target_level"] = level
                    record["_target_category"] = target
                    extracted += 1
                    yield record
                counts[target] = {"candidates": available, "extracted": extracted}

    return records(), summary


def extract_candidates(
    taxonomy_path: Path,
    upstream_path: Path,
    **kwargs: object,
) -> tuple[list[JsonObject], JsonObject]:
    """Compatibility wrapper; the CLI uses stream_candidates for bounded memory."""
    records, summary = stream_candidates(taxonomy_path, upstream_path, **kwargs)
    return list(records), summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于 taxonomy 的 Population 与候选诊断工具")
    parser.add_argument("--task", choices=["distribution", "extract"], required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, help="上游聚类全集 JSONL")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--level", choices=["l1", "l2"], default="l1")
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--target-n", type=non_negative_int, default=None)
    parser.add_argument("--no-dedup", action="store_true")
    parser.add_argument("--targets", default="")
    parser.add_argument("--per-category", type=non_negative_int, default=0)
    parser.add_argument("--exclude", type=Path, nargs="*", default=[])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    parser.add_argument("--log-file", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log = setup_logger(__name__, args.log_level, args.log_file)
    try:
        taxonomy = args.taxonomy.expanduser().resolve()
        upstream = args.input.expanduser().resolve()
        output = args.output.expanduser().resolve()
        if args.task == "distribution":
            snapshot = distribution_snapshot(
                taxonomy,
                upstream,
                level=args.level,
                alpha=args.alpha,
                target_n=args.target_n,
                deduplicate=not args.no_dedup,
                cache_root=args.cache_root,
                seed=args.seed,
            )
            write_json(output, snapshot)
        else:
            targets = [value.strip() for value in args.targets.split(",") if value.strip()]
            if not targets:
                raise ValueError("extract 任务必须提供 --targets")
            records, summary = stream_candidates(
                taxonomy,
                upstream,
                level=args.level,
                targets=targets,
                per_category=args.per_category,
                exclude_paths=[path.expanduser().resolve() for path in args.exclude],
                seed=args.seed,
                deduplicate=not args.no_dedup,
                cache_root=args.cache_root,
            )
            write_jsonl(output, records)
            write_json(output.with_suffix(output.suffix + ".summary.json"), summary)
        log.info("输出完成: %s", output)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        log.error("taxonomy 诊断失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
