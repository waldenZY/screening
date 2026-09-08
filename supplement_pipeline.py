"""P1 Coverage construction with resumable, adaptive incremental sampling."""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .core import (
    SET_FILES,
    JsonObject,
    build_cluster_candidate,
    ensure_within,
    get_decision,
    iter_jsonl,
    load_jsonl,
    load_prompt_template,
    non_negative_float,
    positive_int,
    prescreen_results_path,
    sample_identity,
    sha256_file,
    supplement_artifact_path,
    validate_source_run_roots,
    write_json,
    write_jsonl,
)
from .cluster_index import (
    CandidateIndex,
    ClusterIndexHandle,
    ensure_cluster_index,
    identity_sha256,
    read_indexed_record,
)
from .coverage import (
    TaxonomyMappings,
    allocate_largest_remainder,
    load_taxonomy_mappings,
)
from .inference import LlmCall, run_inference_batch
from .llm_client import load_llm_config, validate_llm_dependency
from .logging_config import setup_logger

logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = PACKAGE_DIR / "artifacts" / "prompts" / "validity_filter_prompt.md"
RoundCallback = Callable[[list[JsonObject]], None]


@dataclass(frozen=True)
class SupplementOutcome:
    complete: bool
    total_deficit: int
    total_filled: int
    engineering_failures: tuple[str, ...] = ()
    fatal_error: str | None = None
    stop_reason: str | None = None
    rounds: tuple[JsonObject, ...] = ()


@dataclass(frozen=True)
class PreparedSupplement:
    mappings: TaxonomyMappings
    population: JsonObject
    entries: tuple[JsonObject, ...]
    task_rows: dict[int, JsonObject]
    excluded_identity_hashes: tuple[str, ...]
    index_handle: ClusterIndexHandle
    pool_stats: JsonObject


def _source_identity_hashes(source_root: Path) -> set[str]:
    identities: set[str] = set()
    for relative_path in SET_FILES.values():
        path = source_root / relative_path
        if path.is_file():
            identities.update(identity_sha256(row) for _, row in iter_jsonl(path))
    return identities


def prepare_supplement_inputs(
    source_root: Path,
    run_root: Path,
    taxonomy_path: Path,
    upstream_path: Path,
    *,
    index_handle: ClusterIndexHandle | None = None,
    cache_root: Path | None = None,
    seed: int = 42,
) -> PreparedSupplement:
    """Prepare small P1 state from the persistent cluster locator index."""
    mappings = load_taxonomy_mappings(taxonomy_path)
    if index_handle is None:
        taxonomy_hash = sha256_file(taxonomy_path)
        default_cache_root = (
            run_root.parents[2] / "_cluster_cache"
            if run_root.parent.name == "runs" and len(run_root.parents) >= 3
            else run_root.parent / "_cluster_cache"
        )
        index_handle = ensure_cluster_index(
            upstream_path,
            mappings,
            taxonomy_sha256=taxonomy_hash,
            cache_root=cache_root or default_cache_root,
            seed=seed,
        )
    population = dict(index_handle.metadata["population"])
    tasks_path = supplement_artifact_path(run_root, "supplement_tasks.jsonl")
    entries = load_jsonl(tasks_path) if tasks_path.is_file() else []
    task_rows: dict[int, JsonObject] = {}
    for entry in entries:
        line = int(entry["source_line"])
        task_rows[line] = read_indexed_record(upstream_path, entry)
    excluded = _source_identity_hashes(source_root)
    excluded.update(str(entry["identity_sha256"]) for entry in entries)
    with CandidateIndex(index_handle) as candidates:
        candidates.exclude(excluded)
        available_by_l1 = candidates.available_by_l1()
    pool_stats: JsonObject = {
        "indexed_candidates": int(population.get("mapped_rows", 0)),
        "excluded_identity_hashes": len(excluded),
        "available_rows": sum(available_by_l1.values()),
        "available_by_l1": available_by_l1,
        "selection_mode": "sqlite_l1_stable_rank",
    }
    return PreparedSupplement(
        mappings=mappings,
        population=population,
        entries=tuple(entries),
        task_rows=task_rows,
        excluded_identity_hashes=tuple(sorted(excluded)),
        index_handle=index_handle,
        pool_stats=pool_stats,
    )


def _resolve_initial_l1(row: JsonObject, mappings: TaxonomyMappings) -> str | None:
    cluster_id = row.get("cluster")
    raw_l1 = str(row.get("L1") or "").strip()
    if cluster_id is not None:
        mapped_l1 = mappings.cluster_to_l1.get(cluster_id)
        if mapped_l1 is None:
            return None
        if raw_l1 and raw_l1 != mapped_l1:
            raise ValueError(
                f"P1 初版记录 L1 {raw_l1!r} 与 cluster {cluster_id!r} 映射 {mapped_l1!r} 冲突"
            )
        return mapped_l1
    return raw_l1 if raw_l1 in mappings.l1_population else None


def _initial_observations(
    source_root: Path,
    results_path: Path,
    mappings: TaxonomyMappings,
) -> tuple[list[JsonObject], Counter[str], Counter[str], JsonObject]:
    rows = {line: row for line, row in iter_jsonl(source_root / SET_FILES["P1"])}
    qualified: list[JsonObject] = []
    terminal_by_l1: Counter[str] = Counter()
    keep_by_l1: Counter[str] = Counter()
    seen_keep: set[tuple[str, str]] = set()
    stats: Counter[str] = Counter()
    for result in load_jsonl(results_path):
        if result.get("set") != "P1":
            continue
        source_line = result.get("source_line")
        if type(source_line) is not int or source_line not in rows:
            raise ValueError("P1 初筛结果无法关联原始记录")
        row = rows[source_line]
        l1 = _resolve_initial_l1(row, mappings)
        if l1 is None:
            stats["unallocated_terminal"] += 1
            if get_decision(result) == "KEEP":
                stats["unallocated_keep"] += 1
            continue
        terminal_by_l1[l1] += 1
        if get_decision(result) != "KEEP":
            continue
        keep_by_l1[l1] += 1
        identity = sample_identity(row)
        if identity in seen_keep:
            stats["duplicate_keep"] += 1
            continue
        seen_keep.add(identity)
        qualified.append(
            {
                "origin": "initial",
                "source_line": source_line,
                "L1": l1,
                "L2": mappings.cluster_to_l2.get(
                    row.get("cluster"),
                    f"{l1}/{str(row.get('L2') or '__L1_ONLY__').strip()}",
                ),
                "cluster": row.get("cluster"),
                "identity": list(identity),
                "record": row,
            }
        )
    return qualified, terminal_by_l1, keep_by_l1, dict(stats)


def _supplement_observations(
    results_path: Path,
    upstream_rows: dict[int, JsonObject],
) -> tuple[list[JsonObject], Counter[str], Counter[str]]:
    qualified: list[JsonObject] = []
    terminal_by_l1: Counter[str] = Counter()
    keep_by_l1: Counter[str] = Counter()
    for result in load_jsonl(results_path) if results_path.is_file() else []:
        line = result.get("source_line")
        l1 = str(result.get("L1") or "")
        if type(line) is not int or line not in upstream_rows or not l1:
            raise ValueError("P1 增量结果缺少合法来源或 L1")
        terminal_by_l1[l1] += 1
        if get_decision(result) != "KEEP":
            continue
        keep_by_l1[l1] += 1
        row = dict(upstream_rows[line])
        row["_target_level"] = "l1"
        row["_target_category"] = l1
        qualified.append(
            {
                "origin": "upstream",
                "source_line": line,
                "round_id": result.get("round_id"),
                "L1": l1,
                "L2": str(result.get("L2") or ""),
                "cluster": result.get("cluster"),
                "identity": list(sample_identity(row)),
                "record": row,
            }
        )
    return qualified, terminal_by_l1, keep_by_l1


def qualified_by_l1(records: list[JsonObject]) -> tuple[Counter[str], list[JsonObject]]:
    """Deduplicate qualified records and return their L1 counts."""
    counts: Counter[str] = Counter()
    unique: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        identity = tuple(str(value) for value in record.get("identity", []))
        if len(identity) != 2:
            identity = sample_identity(record.get("record", {}))
        if identity in seen:
            continue
        seen.add(identity)
        counts[str(record["L1"])] += 1
        unique.append(record)
    return counts, unique


def quota_deficits(quotas: dict[str, int], qualified: Counter[str]) -> dict[str, int]:
    return {
        l1: max(0, int(quota) - int(qualified.get(l1, 0)))
        for l1, quota in quotas.items()
        if quota > 0
    }


def estimate_pass_rates(
    l1_names: list[str],
    initial_terminal: Counter[str],
    initial_keep: Counter[str],
    supplement_terminal: Counter[str],
    supplement_keep: Counter[str],
    *,
    default_rate: float,
    prior_strength: float,
    min_rate: float,
    max_rate: float,
    global_initial_terminal: int | None = None,
    global_initial_keep: int | None = None,
) -> tuple[dict[str, float], float]:
    """Return empirical-Bayes pass-rate estimates for the next P1 round."""
    initial_n = (
        sum(initial_terminal.values())
        if global_initial_terminal is None
        else global_initial_terminal
    )
    initial_keep_n = (
        sum(initial_keep.values())
        if global_initial_keep is None
        else global_initial_keep
    )
    global_rate = initial_keep_n / initial_n if initial_n else default_rate
    global_rate = min(max(global_rate, min_rate), max_rate)
    rates: dict[str, float] = {}
    for l1 in l1_names:
        initial_rate = (
            initial_keep.get(l1, 0) + prior_strength * global_rate
        ) / (initial_terminal.get(l1, 0) + prior_strength)
        supplement_n = supplement_terminal.get(l1, 0)
        rate = (
            supplement_keep.get(l1, 0) + prior_strength * initial_rate
        ) / (supplement_n + prior_strength)
        rates[l1] = min(max(rate, min_rate), max_rate)
    return rates, global_rate


def _task_entry(candidate: JsonObject, round_id: int, prompt_hash: str) -> JsonObject:
    line = int(candidate["source_line"])
    return {
        "filter_id": f"P1_UP_{line:012d}",
        "set": "P1",
        "origin": "upstream",
        "source_line": line,
        "round_id": round_id,
        "L1": candidate["L1"],
        "L2": candidate["L2"],
        "cluster": candidate["cluster"],
        "identity_sha256": candidate["identity_sha256"],
        "byte_offset": candidate["byte_offset"],
        "byte_length": candidate["byte_length"],
        "record_sha256": candidate["record_sha256"],
        "candidate": build_cluster_candidate(candidate["record"]),
        "prompt_sha256": prompt_hash,
    }


def _materialize_tasks(
    entries: list[JsonObject],
    rows_by_line: dict[int, JsonObject],
    template: str,
    prompt_hash: str,
) -> list[JsonObject]:
    tasks: list[JsonObject] = []
    for entry in entries:
        if entry.get("prompt_sha256") != prompt_hash:
            raise ValueError("补样任务账本与当前 Prompt 快照不兼容")
        line = int(entry["source_line"])
        row = rows_by_line.get(line)
        if row is None or identity_sha256(row) != entry.get("identity_sha256"):
            raise ValueError("补样任务账本无法稳定关联上游记录")
        candidate = entry.get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError("补样任务账本缺少规范化候选")
        task = dict(entry)
        task["prompt"] = template.replace(
            "{CANDIDATE_JSON}", json.dumps(candidate, ensure_ascii=False, indent=2)
        )
        tasks.append(task)
    return tasks


def _round_summary(
    round_id: int,
    entries: list[JsonObject],
    results_path: Path,
    requested: dict[str, int],
    rates: dict[str, float],
    status: str = "completed",
    failures: tuple[str, ...] = (),
    error: str | None = None,
) -> JsonObject:
    ids = {str(entry["filter_id"]) for entry in entries}
    results = [
        row
        for row in (load_jsonl(results_path) if results_path.is_file() else [])
        if str(row.get("filter_id")) in ids
    ]
    keep_by_l1 = Counter(
        str(row.get("L1")) for row in results if get_decision(row) == "KEEP"
    )
    return {
        "round_id": round_id,
        "status": status,
        "requested_by_l1": dict(requested),
        "estimated_pass_rate_by_l1": {
            l1: rates[l1] for l1 in requested if l1 in rates
        },
        "candidates": len(entries),
        "terminal": len(results),
        "keep": sum(keep_by_l1.values()),
        "keep_by_l1": dict(keep_by_l1),
        "engineering_failures": list(failures),
        "error": error,
    }


def _write_summary(
    run_root: Path,
    *,
    population: JsonObject,
    quotas: dict[str, int],
    shares: dict[str, float],
    params: JsonObject,
    initial_counts: Counter[str],
    initial_deficits: dict[str, int],
    final_counts: Counter[str],
    final_deficits: dict[str, int],
    initial_stats: JsonObject,
    rounds: list[JsonObject],
    pool_stats: JsonObject,
    stop_reason: str,
    complete: bool,
    final_rates: dict[str, float],
    global_initial_rate: float,
    engineering_failures: tuple[str, ...] = (),
) -> JsonObject:
    summary: JsonObject = {
        "schema_version": 3,
        "mode": "p1_coverage",
        "enabled": True,
        "complete": complete,
        "params": params,
        "population": population,
        "target_share": shares,
        "quota_by_l1": quotas,
        "initial_qualified_by_l1": dict(initial_counts),
        "initial_deficit_by_l1": initial_deficits,
        "initial_mapping": initial_stats,
        "global_initial_pass_rate": global_initial_rate,
        "final_estimated_pass_rate_by_l1": final_rates,
        "final_qualified_by_l1": dict(final_counts),
        "final_deficit_by_l1": final_deficits,
        "rounds": rounds,
        "upstream_pool": pool_stats,
        "total_deficit": sum(initial_deficits.values()),
        "total_filled": sum(initial_deficits.values()) - sum(final_deficits.values()),
        "final_unresolved": {key: value for key, value in final_deficits.items() if value},
        "stop_reason": stop_reason,
        "engineering_failures": list(engineering_failures),
    }
    write_json(supplement_artifact_path(run_root, "supplement_summary.json"), summary)
    return summary


def run_supplement(
    source_root: Path,
    run_root: Path,
    *,
    taxonomy_path: Path,
    upstream_path: Path,
    prompt_path: Path,
    alpha: float = 0.5,
    target_n: int | None = None,
    seed: int = 42,
    max_rounds: int = 3,
    default_pass_rate: float = 0.55,
    pass_rate_prior_strength: float = 10.0,
    min_pass_rate: float = 0.10,
    max_pass_rate: float = 0.95,
    safety_factor: float = 1.10,
    model: str,
    workers: int = 3,
    sleep_sec: float = 1.0,
    max_retries: int = 3,
    llm_options: JsonObject,
    stage_logger: logging.Logger | None = None,
    llm_call: LlmCall,
    round_callback: RoundCallback | None = None,
    prepared: PreparedSupplement | None = None,
) -> SupplementOutcome:
    """Build or resume P1 Coverage; business shortfalls remain publishable."""
    source_root, run_root = validate_source_run_roots(source_root, run_root)
    active_logger = stage_logger or logger
    taxonomy_path = taxonomy_path.expanduser().resolve()
    upstream_path = upstream_path.expanduser().resolve()
    if not taxonomy_path.is_file() or not upstream_path.is_file():
        raise FileNotFoundError("taxonomy 或上游聚类全集不存在")
    if not 0 < default_pass_rate <= 1:
        raise ValueError("default_pass_rate 必须在 (0, 1] 内")
    if pass_rate_prior_strength <= 0:
        raise ValueError("pass_rate_prior_strength 必须大于 0")
    if not 0 < min_pass_rate <= max_pass_rate <= 1:
        raise ValueError("通过率上下界必须满足 0 < min <= max <= 1")
    if safety_factor <= 0 or max_rounds < 0:
        raise ValueError("safety_factor 必须大于 0，max_rounds 不能小于 0")

    template = load_prompt_template(prompt_path)
    prompt_hash = sha256_file(prompt_path)
    if prepared is None:
        prepared = prepare_supplement_inputs(
            source_root,
            run_root,
            taxonomy_path,
            upstream_path,
            seed=seed,
        )
    mappings = prepared.mappings
    population = prepared.population
    if population.get("pct_differences"):
        active_logger.warning(
            "taxonomy pct 与 total_size 重算结果存在差异: %s",
            population["pct_differences"],
        )
    if population.get("invalid_frequency_rows"):
        active_logger.warning(
            "cluster 全集有 %d 条非法 frequency；frequency 不参与配额",
            population["invalid_frequency_rows"],
        )
    if target_n is None:
        target_n = sum(1 for _ in iter_jsonl(source_root / SET_FILES["P1"]))
    quotas, shares = allocate_largest_remainder(
        population["l1_counts"], int(target_n), float(alpha)
    )

    initial_results = prescreen_results_path(run_root, "P1")
    if not initial_results.is_file():
        raise FileNotFoundError(f"缺少 P1 统一质检结果: {initial_results}")
    initial, initial_terminal, initial_keep_counts, initial_stats = _initial_observations(
        source_root, initial_results, mappings
    )
    global_initial_counts = {
        "global_initial_terminal": (
            sum(initial_terminal.values())
            + int(initial_stats.get("unallocated_terminal", 0))
        ),
        "global_initial_keep": (
            sum(initial_keep_counts.values())
            + int(initial_stats.get("unallocated_keep", 0))
        ),
    }
    initial_counts, initial = qualified_by_l1(initial)
    initial_deficits = quota_deficits(quotas, initial_counts)

    tasks_path = supplement_artifact_path(run_root, "supplement_tasks.jsonl")
    results_path = supplement_artifact_path(run_root, "supplement_results.jsonl")
    attempts_path = supplement_artifact_path(run_root, "supplement_attempts.jsonl")
    entries = list(prepared.entries)
    if not results_path.is_file():
        write_jsonl(results_path, [])
    task_rows = dict(prepared.task_rows)
    pool_stats = dict(prepared.pool_stats)
    candidate_index = CandidateIndex(prepared.index_handle)
    candidate_index.exclude(prepared.excluded_identity_hashes)
    rounds: list[JsonObject] = []
    stop_reason = "max_rounds"

    for round_id in range(1, max_rounds + 1):
        supplement, supplement_terminal, supplement_keep_counts = _supplement_observations(
            results_path, task_rows
        )
        current_counts, _ = qualified_by_l1(initial + supplement)
        deficits = quota_deficits(quotas, current_counts)
        rates, global_rate = estimate_pass_rates(
            list(quotas),
            initial_terminal,
            initial_keep_counts,
            supplement_terminal,
            supplement_keep_counts,
            default_rate=default_pass_rate,
            prior_strength=pass_rate_prior_strength,
            min_rate=min_pass_rate,
            max_rate=max_pass_rate,
            **global_initial_counts,
        )
        if not any(deficits.values()):
            stop_reason = "quota_filled"
            break

        round_entries = [
            entry for entry in entries if int(entry.get("round_id", 0)) == round_id
        ]
        requested: dict[str, int] = {}
        if not round_entries:
            for l1, deficit in deficits.items():
                if deficit <= 0:
                    continue
                request_count = math.ceil(deficit / rates[l1] * safety_factor)
                requested[l1] = request_count
                chosen = candidate_index.take(l1, request_count)
                for item in chosen:
                    item["record"] = read_indexed_record(upstream_path, item)
                new_entries = [_task_entry(item, round_id, prompt_hash) for item in chosen]
                round_entries.extend(new_entries)
                task_rows.update({int(item["source_line"]): item["record"] for item in chosen})
            if not round_entries:
                stop_reason = "candidate_exhausted"
                break
            entries.extend(round_entries)
            write_jsonl(tasks_path, entries)
        else:
            requested = dict(Counter(str(entry["L1"]) for entry in round_entries))

        running = _round_summary(
            round_id, round_entries, results_path, requested, rates, status="running"
        )
        rounds = [item for item in rounds if item.get("round_id") != round_id] + [running]
        rounds.sort(key=lambda item: int(item["round_id"]))
        if round_callback:
            round_callback(rounds)

        tasks = _materialize_tasks(round_entries, task_rows, template, prompt_hash)
        outcome = run_inference_batch(
            tasks,
            stage=f"p1_coverage_round_{round_id}",
            attempts_path=attempts_path,
            results_path=results_path,
            model=model,
            workers=workers,
            sleep_sec=sleep_sec,
            max_retries=max_retries,
            llm_options=llm_options,
            selected_sets=["P1"],
            logger=active_logger,
            llm_call=llm_call,
        )
        if not outcome.complete:
            failed = _round_summary(
                round_id,
                round_entries,
                results_path,
                requested,
                rates,
                status="incomplete",
                failures=outcome.engineering_failures,
                error=outcome.fatal_error,
            )
            rounds = [item for item in rounds if item.get("round_id") != round_id] + [failed]
            rounds.sort(key=lambda item: int(item["round_id"]))
            if round_callback:
                round_callback(rounds)
            supplement, supplement_terminal, supplement_keep_counts = _supplement_observations(
                results_path, task_rows
            )
            final_counts, _ = qualified_by_l1(initial + supplement)
            final_deficits = quota_deficits(quotas, final_counts)
            final_rates, global_rate = estimate_pass_rates(
                list(quotas), initial_terminal, initial_keep_counts,
                supplement_terminal, supplement_keep_counts,
                default_rate=default_pass_rate,
                prior_strength=pass_rate_prior_strength,
                min_rate=min_pass_rate,
                max_rate=max_pass_rate,
                **global_initial_counts,
            )
            _write_summary(
                run_root,
                population=population,
                quotas=quotas,
                shares=shares,
                params={
                    "target_n": target_n, "alpha": alpha, "seed": seed,
                    "max_rounds": max_rounds, "default_pass_rate": default_pass_rate,
                    "pass_rate_prior_strength": pass_rate_prior_strength,
                    "min_pass_rate": min_pass_rate, "max_pass_rate": max_pass_rate,
                    "safety_factor": safety_factor,
                },
                initial_counts=initial_counts,
                initial_deficits=initial_deficits,
                final_counts=final_counts,
                final_deficits=final_deficits,
                initial_stats=initial_stats,
                rounds=rounds,
                pool_stats=pool_stats,
                stop_reason="engineering_failure",
                complete=False,
                final_rates=final_rates,
                global_initial_rate=global_rate,
                engineering_failures=outcome.engineering_failures,
            )
            candidate_index.close()
            return SupplementOutcome(
                False, sum(initial_deficits.values()),
                sum(initial_deficits.values()) - sum(final_deficits.values()),
                outcome.engineering_failures, outcome.fatal_error,
                "engineering_failure", tuple(rounds),
            )

        completed = _round_summary(
            round_id, round_entries, results_path, requested, rates
        )
        rounds = [item for item in rounds if item.get("round_id") != round_id] + [completed]
        rounds.sort(key=lambda item: int(item["round_id"]))
        if round_callback:
            round_callback(rounds)
        active_logger.info(
            "P1 Coverage round=%d candidates=%d keep=%d",
            round_id, completed["candidates"], completed["keep"],
        )

    supplement, supplement_terminal, supplement_keep_counts = _supplement_observations(
        results_path, task_rows
    )
    final_counts, _ = qualified_by_l1(initial + supplement)
    final_deficits = quota_deficits(quotas, final_counts)
    final_rates, global_rate = estimate_pass_rates(
        list(quotas), initial_terminal, initial_keep_counts,
        supplement_terminal, supplement_keep_counts,
        default_rate=default_pass_rate,
        prior_strength=pass_rate_prior_strength,
        min_rate=min_pass_rate,
        max_rate=max_pass_rate,
        **global_initial_counts,
    )
    if not any(final_deficits.values()):
        stop_reason = "quota_filled"
    elif max_rounds == 0:
        stop_reason = "max_rounds"
    pool_stats["remaining_by_l1"] = candidate_index.available_by_l1()
    pool_stats["remaining_rows"] = sum(pool_stats["remaining_by_l1"].values())
    _write_summary(
        run_root,
        population=population,
        quotas=quotas,
        shares=shares,
        params={
            "target_n": target_n, "alpha": alpha, "seed": seed,
            "max_rounds": max_rounds, "default_pass_rate": default_pass_rate,
            "pass_rate_prior_strength": pass_rate_prior_strength,
            "min_pass_rate": min_pass_rate, "max_pass_rate": max_pass_rate,
            "safety_factor": safety_factor,
        },
        initial_counts=initial_counts,
        initial_deficits=initial_deficits,
        final_counts=final_counts,
        final_deficits=final_deficits,
        initial_stats=initial_stats,
        rounds=rounds,
        pool_stats=pool_stats,
        stop_reason=stop_reason,
        complete=True,
        final_rates=final_rates,
        global_initial_rate=global_rate,
    )
    candidate_index.close()
    return SupplementOutcome(
        True, sum(initial_deficits.values()),
        sum(initial_deficits.values()) - sum(final_deficits.values()),
        stop_reason=stop_reason, rounds=tuple(rounds),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 P1 Coverage 增量构建")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--upstream-cluster-population", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--target-n", type=positive_int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--default-pass-rate", type=float, default=0.55)
    parser.add_argument("--pass-rate-prior-strength", type=float, default=10.0)
    parser.add_argument("--min-pass-rate", type=float, default=0.10)
    parser.add_argument("--max-pass-rate", type=float, default=0.95)
    parser.add_argument("--safety-factor", type=float, default=1.10)
    parser.add_argument("--workers", type=positive_int, default=3)
    parser.add_argument("--sleep", type=non_negative_float, default=1.0)
    parser.add_argument("--max-retries", type=positive_int, default=3)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    parser.add_argument("--log-file", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root, run_root = validate_source_run_roots(args.source_root, args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    log_path = ensure_within(
        args.log_file or run_root / "logs" / "pipeline.log", run_root, "日志文件"
    )
    active_logger = setup_logger(
        __name__, args.log_level, log_path, run_id=run_root.name, stage="p1_coverage"
    )
    try:
        llm_config = load_llm_config()
        validate_llm_dependency()
        outcome = run_supplement(
            source_root, run_root,
            taxonomy_path=args.taxonomy,
            upstream_path=args.upstream_cluster_population,
            prompt_path=args.prompt,
            alpha=args.alpha,
            target_n=args.target_n,
            seed=args.seed,
            max_rounds=args.max_rounds,
            default_pass_rate=args.default_pass_rate,
            pass_rate_prior_strength=args.pass_rate_prior_strength,
            min_pass_rate=args.min_pass_rate,
            max_pass_rate=args.max_pass_rate,
            safety_factor=args.safety_factor,
            model=llm_config.model,
            workers=args.workers,
            sleep_sec=args.sleep,
            max_retries=args.max_retries,
            llm_options=llm_config.options(),
            stage_logger=active_logger,
            llm_call=llm_config.bind(),
        )
        return 0 if outcome.complete else 2
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        active_logger.error("P1 Coverage 失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
