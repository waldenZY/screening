"""P0/P1/P2 orchestration, admission, P1 supplement, and final construction."""

from __future__ import annotations

import json
import inspect
import logging
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..candidate.index import CanonicalCandidateIndex, CanonicalIndexHandle
from ..candidate.dedup import DedupConfig, load_or_build_idf_snapshot, validate_idf_snapshot, RULES_SHA256
from ..shared.json_io import JsonObject, load_jsonl, sha256_file, write_json, write_jsonl
from ..shared.taxonomy import TaxonomyMappings
from ..candidate.sampling import (
    CandidateSampler,
    SamplingConfig,
    P0SamplingConfig,
    P1SamplingConfig,
    P2SamplingConfig,
    allocate_sqrt_quotas,
    deduplicate_branch_candidates,
)
from ..candidate.p2_adaptive import (
    P2_ALGORITHM,
    P2_SOURCE_NAMES,
    AdaptiveP2Sampler,
    allocate_by_weights,
    select_final_p2,
    source_yield_rates,
)
from ..delivery.legacy import LEGACY_MAPPING_VERSION, export_legacy_branch
from ..delivery.eligibility import delivery_labels
from ..delivery.report import RUN_REPORT_SCHEMA_VERSION, write_run_report
from ..delivery.screened import (
    build_screened_canonical_record,
    validate_screened_canonical_records,
)
from ..quality.two_stage import (
    CHALLENGE_TYPES,
    TwoStageQualityRunner,
    challenge_passes,
    stage1_passes,
)
from .checkpoints import CheckpointStore


BUILD_CONFIG_VERSION = 5


@dataclass(frozen=True)
class P1SupplementConfig:
    max_rounds: int = 3
    default_pass_rate: float = 0.55
    pass_rate_prior_strength: float = 10.0
    minimum_pass_rate: float = 0.10
    maximum_pass_rate: float = 0.95
    safety_factor: float = 1.1

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_rounds, bool)
            or not isinstance(self.max_rounds, int)
            or self.max_rounds < 0
        ):
            raise ValueError("max_rounds 必须是非负整数")
        if not 0 < self.minimum_pass_rate <= self.maximum_pass_rate <= 1:
            raise ValueError("补采通过率范围非法")
        if not self.minimum_pass_rate <= self.default_pass_rate <= self.maximum_pass_rate:
            raise ValueError("default_pass_rate 必须位于通过率范围内")
        if self.pass_rate_prior_strength <= 0 or self.safety_factor <= 0:
            raise ValueError("补采先验强度和安全系数必须为正数")


@dataclass(frozen=True)
class P2SupplementConfig:
    max_rounds: int = 3
    default_yield: float = 0.10
    type_default_yield: float = 0.0125
    prior_strength: float = 10.0
    minimum_yield: float = 0.005
    safety_factor: float = 1.1
    exploration_share: float = 0.20
    max_sampled: int = 15000
    max_tail_sampled: int = 1000
    tail_batch_size: int = 250
    max_stalled_batches: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int) or self.max_rounds < 0:
            raise ValueError("P2 max_rounds 必须是非负整数")
        numeric = {
            "default_yield": self.default_yield,
            "type_default_yield": self.type_default_yield,
            "prior_strength": self.prior_strength,
            "minimum_yield": self.minimum_yield,
            "safety_factor": self.safety_factor,
            "exploration_share": self.exploration_share,
        }
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in numeric.values()
        ):
            raise ValueError("P2 补采数值参数必须是数字")
        if not 0 < self.minimum_yield <= self.default_yield <= 1:
            raise ValueError("P2 总产出率参数非法")
        if not self.minimum_yield <= self.type_default_yield <= self.default_yield:
            raise ValueError("P2 类型产出率参数非法")
        if self.prior_strength <= 0 or self.safety_factor <= 0:
            raise ValueError("P2 先验强度和安全系数必须为正数")
        if not 0 <= self.exploration_share <= 1:
            raise ValueError("P2 exploration_share 必须位于 [0, 1]")
        for name in ("max_sampled", "tail_batch_size", "max_stalled_batches"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"P2 {name} 必须是正整数")
        if (
            isinstance(self.max_tail_sampled, bool)
            or not isinstance(self.max_tail_sampled, int)
            or self.max_tail_sampled < 0
        ):
            raise ValueError("P2 max_tail_sampled 必须是非负整数")
        if self.max_tail_sampled > self.max_sampled:
            raise ValueError("P2 max_tail_sampled 不能大于 max_sampled")


@dataclass(frozen=True)
class BuildConfig:
    config_version: int = BUILD_CONFIG_VERSION
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    p1_supplement: P1SupplementConfig = field(default_factory=P1SupplementConfig)
    p2_supplement: P2SupplementConfig = field(default_factory=P2SupplementConfig)

    def __post_init__(self) -> None:
        if self.config_version != BUILD_CONFIG_VERSION:
            raise ValueError(f"不支持的 P012 构建配置版本: {self.config_version}")


@dataclass(frozen=True)
class BuildOutcome:
    records: Mapping[str, tuple[JsonObject, ...]]
    summary: JsonObject
    errors: Mapping[str, str] = field(default_factory=dict)


def _only(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(mapping) - allowed
    if extra:
        raise ValueError(f"{label} 包含未知字段: {sorted(extra)}")


def parse_build_config(value: Mapping[str, Any]) -> BuildConfig:
    """Parse the versioned sampling/supplement portion of a configuration."""
    if not isinstance(value, dict):
        raise ValueError("P012 构建配置必须是 JSON 对象")
    _only(value, {"config_version", "sampling", "p1_supplement", "p2_supplement"}, "配置")
    sampling = value.get("sampling", {})
    supplement = value.get("p1_supplement", {})
    p2_supplement = value.get("p2_supplement", {})
    if not isinstance(sampling, dict) or not isinstance(supplement, dict) or not isinstance(p2_supplement, dict):
        raise ValueError("sampling、p1_supplement 和 p2_supplement 必须是对象")
    _only(sampling, {"p0", "p1", "p2", "dedup"}, "sampling")
    dedup_raw = sampling.get("dedup", {})
    if not isinstance(dedup_raw, dict):
        raise ValueError("sampling.dedup must be an object")
    try:
        dedup_config = DedupConfig(**dedup_raw)
    except TypeError as exc:
        raise ValueError(f"sampling.dedup contains invalid fields: {exc}") from exc
    sections: dict[str, Any] = {}
    for key, cls in (("p0", P0SamplingConfig), ("p1", P1SamplingConfig), ("p2", P2SamplingConfig)):
        raw = sampling.get(key, {})
        if not isinstance(raw, dict):
            raise ValueError(f"sampling.{key} 必须是对象")
        try:
            sections[key] = cls(**raw)
        except TypeError as exc:
            raise ValueError(f"sampling.{key} 字段非法: {exc}") from exc
    try:
        supplement_config = P1SupplementConfig(**supplement)
    except TypeError as exc:
        raise ValueError(f"p1_supplement 字段非法: {exc}") from exc
    try:
        p2_supplement_config = P2SupplementConfig(**p2_supplement)
    except TypeError as exc:
        raise ValueError(f"p2_supplement 字段非法: {exc}") from exc
    return BuildConfig(
        config_version=value.get("config_version", BUILD_CONFIG_VERSION),
        sampling=SamplingConfig(**sections, dedup=dedup_config),
        p1_supplement=supplement_config,
        p2_supplement=p2_supplement_config,
    )


def _materialize(
    handle: CanonicalIndexHandle,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    locators: Sequence[JsonObject],
    cache: dict[str, JsonObject],
) -> list[JsonObject]:
    result: list[JsonObject] = []
    with CanonicalCandidateIndex(handle) as index:
        for locator in locators:
            identity = locator["source_identity_sha256"]
            if identity not in cache:
                cache[identity] = index.materialize(source_path, locator, taxonomy)
            result.append(cache[identity])
    return result


def _sample_branch(
    set_name: str,
    handle: CanonicalIndexHandle,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    config: SamplingConfig,
    excluded: Sequence[str] = (),
    requested_by_cluster: Mapping[Any, int] | None = None,
) -> list[JsonObject]:
    with CanonicalCandidateIndex(handle) as index:
        sampler = CandidateSampler(index, source_path, taxonomy, config)
        if set_name == "P0":
            return sampler.sample_p0(excluded, requested_by_cluster=requested_by_cluster)
        if set_name == "P1":
            return sampler.sample_p1()
        raise ValueError(f"初始静态采样只支持 P0/P1，收到: {set_name}")


def _deduplicate(
    set_name: str,
    candidates: Sequence[JsonObject],
    existing: Sequence[JsonObject] = (),
    *,
    config: DedupConfig,
    idf_snapshot: Mapping[str, Any],
    audit: list[JsonObject] | None = None,
) -> list[JsonObject]:
    return deduplicate_branch_candidates(
        set_name,
        candidates,
        existing,
        config=config,
        idf_snapshot=idf_snapshot,
        audit=audit,
    )


def _passing(
    candidates: Sequence[JsonObject],
    results: Mapping[str, JsonObject],
) -> list[JsonObject]:
    return [
        candidate for candidate in candidates
        if stage1_passes(results[candidate["source_identity_sha256"]])
    ]


def _l2_counts(candidates: Sequence[JsonObject]) -> Counter[str]:
    return Counter(candidate["taxonomy"]["l2_key"] for candidate in candidates)


def _update_cluster_feedback(
    feedback: dict[Any, list[int]],
    candidates: Sequence[JsonObject],
    results: Mapping[str, JsonObject],
) -> None:
    for candidate in candidates:
        cluster = candidate["taxonomy"]["cluster_id"]
        counts = feedback.setdefault(cluster, [0, 0])
        counts[0] += 1
        counts[1] += int(stage1_passes(results[candidate["source_identity_sha256"]]))


def _pass_rates(
    l2_keys: Sequence[str],
    initial_terminal: Mapping[str, int],
    initial_pass: Mapping[str, int],
    supplement_terminal: Mapping[str, int],
    supplement_pass: Mapping[str, int],
    config: P1SupplementConfig,
) -> dict[str, float]:
    global_terminal = sum(initial_terminal.values())
    global_pass = sum(initial_pass.values())
    global_rate = global_pass / global_terminal if global_terminal else config.default_pass_rate
    global_rate = min(max(global_rate, config.minimum_pass_rate), config.maximum_pass_rate)
    result: dict[str, float] = {}
    for l2_key in l2_keys:
        initial_rate = (
            initial_pass.get(l2_key, 0) + config.pass_rate_prior_strength * global_rate
        ) / (initial_terminal.get(l2_key, 0) + config.pass_rate_prior_strength)
        rate = (
            supplement_pass.get(l2_key, 0)
            + config.pass_rate_prior_strength * initial_rate
        ) / (supplement_terminal.get(l2_key, 0) + config.pass_rate_prior_strength)
        result[l2_key] = min(
            max(rate, config.minimum_pass_rate), config.maximum_pass_rate
        )
    return result


def _branch_audit(
    set_name: str,
    sampled: int,
    deduplicated: int,
    candidates: Sequence[JsonObject],
    stage1: Mapping[str, JsonObject],
) -> JsonObject:
    rejected = Counter()
    for candidate in candidates:
        result = stage1[candidate["source_identity_sha256"]]
        if not stage1_passes(result):
            rejected["/".join(result[key] for key in (
                "category_fit", "semantic_completeness", "intent_clarity", "validity"
            ))] += 1
    return {
        "set": set_name,
        "sampled": sampled,
        "after_dedup": deduplicated,
        "stage1_pass": deduplicated - sum(rejected.values()),
        "stage1_rejected": sum(rejected.values()),
        "stage1_rejection_combinations": dict(sorted(rejected.items())),
    }


def _candidate_task(candidate: Mapping[str, Any], set_name: str, round_id: int) -> JsonObject:
    source = candidate["source"]
    taxonomy = candidate["taxonomy"]
    identity = candidate["source_identity_sha256"]
    return {
        "task_id": f"{set_name}:{round_id}:{identity}",
        "set": set_name,
        "origin": "initial_wave" if round_id == 0 else "supplement_wave",
        "round_id": round_id,
        "source_identity_sha256": identity,
        "conversation_sha256": candidate["conversation_sha256"],
        "source_line": source["source_line"],
        "byte_offset": source["byte_offset"],
        "byte_length": source["byte_length"],
        "record_sha256": source["record_sha256"],
        "cluster_id": taxonomy["cluster_id"],
        "l2_key": taxonomy["l2_key"],
    }


def _merge_candidate_tasks(path: Path, tasks: Sequence[JsonObject]) -> None:
    existing: dict[str, JsonObject] = {}
    if path.is_file():
        for task in load_jsonl(path):
            task_id = str(task.get("task_id", ""))
            if not task_id or task_id in existing:
                raise ValueError(f"候选任务账本 {path} 非法或重复")
            existing[task_id] = task
    for task in tasks:
        task_id = str(task["task_id"])
        if task_id in existing and existing[task_id] != task:
            raise ValueError(f"候选任务 {task_id} 与已冻结账本不一致")
        existing[task_id] = task
    write_jsonl(path, [existing[key] for key in sorted(existing)])


def _quality_task_id(
    runner: Any,
    stage: str,
    candidate: Mapping[str, Any],
    stage1_result: Mapping[str, Any] | None = None,
) -> str:
    task_id = getattr(runner, "task_id", None)
    if callable(task_id):
        return str(task_id(stage, candidate, stage1_result))
    # Test doubles may not implement the persistent cache contract.  Keep their
    # screened fixtures deterministic without pretending to reference a ledger.
    return f"{stage}:{candidate['conversation_sha256']}:{candidate['taxonomy']['l2_key']}"


def _run_quality(
    runner: Any,
    method_name: str,
    candidates: Sequence[JsonObject],
    branch: str,
    stage1: Mapping[str, JsonObject] | None = None,
) -> dict[str, JsonObject]:
    """Use branch-aware scheduling when the supplied runner supports it."""
    method = getattr(runner, method_name)
    args = (candidates,) if stage1 is None else (candidates, stage1)
    if "branch" in inspect.signature(method).parameters:
        return method(*args, branch=branch)
    return method(*args)


def _p2_routing_weights(
    diagnostics: Mapping[str, Any],
    total_rates: Mapping[str, float],
    type_rates: Mapping[str, Mapping[str, float]],
    exploration_share: float,
) -> dict[str, float]:
    total_gap = int(diagnostics["target_gap"])
    deficits = diagnostics["soft_min_deficit_by_primary"]
    scores = {
        source: total_gap * total_rates[source] + sum(
            int(deficits[challenge_type]) * type_rates[source][challenge_type]
            for challenge_type in CHALLENGE_TYPES
        )
        for source in P2_SOURCE_NAMES
    }
    non_exploration = [source for source in P2_SOURCE_NAMES if source != "exploration"]
    score_sum = sum(scores[source] for source in non_exploration)
    if score_sum <= 0:
        base = (1.0 - exploration_share) / len(non_exploration)
        return {**{source: base for source in non_exploration}, "exploration": exploration_share}
    return {
        **{
            source: (1.0 - exploration_share) * scores[source] / score_sum
            for source in non_exploration
        },
        "exploration": exploration_share,
    }


def _p2_wave_budget(
    diagnostics: Mapping[str, Any],
    total_rates: Mapping[str, float],
    type_rates: Mapping[str, Mapping[str, float]],
    safety_factor: float,
) -> int:
    total_gap = int(diagnostics["target_gap"])
    total_need = 0 if total_gap == 0 else math.ceil(total_gap / max(total_rates.values()))
    deficits = diagnostics["soft_min_deficit_by_primary"]
    type_need = sum(
        math.ceil(int(deficits[challenge_type]) / max(
            type_rates[source][challenge_type] for source in P2_SOURCE_NAMES
        ))
        for challenge_type in CHALLENGE_TYPES
        if int(deficits[challenge_type]) > 0
    )
    return math.ceil(max(total_need, type_need) * safety_factor)


def _updated_p2_stalled_batches(
    previous_deficit: int,
    current_deficit: int,
    stalled_batches: int,
) -> int:
    """Count consecutive tail batches that do not reduce aggregate deficit."""
    return stalled_batches + 1 if current_deficit >= previous_deficit else 0


def _p2_next_action(
    selection: Mapping[str, Any],
    *,
    sampled: bool,
    wave: int,
    wave_sampled: int,
    wave_budget: int,
    total_sampled: int,
    tail_sampled: int,
    stalled_batches: int,
    supplement: P2SupplementConfig,
) -> str:
    deficits = selection["soft_min_deficit_by_primary"]
    if selection["target_gap"] == 0 and not any(deficits.values()):
        return "target_and_soft_mins_met"
    if not sampled:
        return "candidate_exhausted"
    if total_sampled >= supplement.max_sampled:
        return "max_sampled"
    if selection["target_gap"] == 0:
        if tail_sampled >= supplement.max_tail_sampled:
            return "max_tail_sampled"
        if stalled_batches >= supplement.max_stalled_batches:
            return "stalled_batches"
        if wave >= supplement.max_rounds and wave_sampled >= wave_budget:
            return "max_rounds"
        return "continue_coverage_tail"
    if wave >= supplement.max_rounds and wave_sampled >= wave_budget:
        return "max_rounds"
    return "continue_target_fill"


def _run_adaptive_p2(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
    checkpoints: CheckpointStore,
    materialized: dict[str, JsonObject],
    logger: logging.Logger,
    idf_snapshot: Mapping[str, Any],
) -> JsonObject:
    """Run resumable Stage1 -> Challenge batches and select official P2 members."""
    complete = checkpoints.load("p2_adaptive_complete")
    if complete is not None:
        p2_root = run_root / "P2"
        p2_root.mkdir(parents=True, exist_ok=True)
        write_json(p2_root / "adaptive_summary.json", complete["diagnostics"])
        return complete

    p2_config = config.sampling.p2
    supplement = config.p2_supplement
    evaluated: list[JsonObject] = []
    stage1_results: dict[str, JsonObject] = {}
    challenge_results: dict[str, JsonObject] = {}
    metadata: dict[str, JsonObject] = {}
    sampled_metadata: dict[str, JsonObject] = {}
    evaluation_rounds: dict[str, int] = {}
    processed: set[str] = set()
    rounds: list[JsonObject] = []
    dedup_events: list[JsonObject] = []
    source_capacity: dict[str, int] = {source: 0 for source in P2_SOURCE_NAMES}
    source_remaining: dict[str, int] = {source: 0 for source in P2_SOURCE_NAMES}
    exhausted = False
    total_sampled = 0
    tail_sampled = 0
    stalled_batches = 0
    budget_stop_reason: str | None = None
    frozen_budget_state = checkpoints.load("p2_budget_state")
    processed_batches = 0

    branch_root = run_root / "P2"
    branch_root.mkdir(parents=True, exist_ok=True)
    logger.info("P2 adaptive start target=%d", p2_config.target_n)
    with CanonicalCandidateIndex(index) as readonly_index:
        sampler = AdaptiveP2Sampler(readonly_index, p2_config)
        source_capacity = sampler.capacities()
        initial_capacity_pending = True
        selected, selection_diag = select_final_p2(evaluated, challenge_results, p2_config)
        for wave in range(0, supplement.max_rounds + 1):
            if selection_diag["target_gap"] == 0 and not any(
                selection_diag["soft_min_deficit_by_primary"].values()
            ):
                break
            total_rates, type_rates = source_yield_rates(
                sampled_metadata,
                challenge_results,
                default_yield=supplement.default_yield,
                type_default_yield=supplement.type_default_yield,
                prior_strength=supplement.prior_strength,
                minimum_yield=supplement.minimum_yield,
            )
            if wave == 0:
                budget = math.ceil(
                    p2_config.target_n / supplement.default_yield * supplement.safety_factor
                )
                weights = dict(p2_config.candidate_source_shares)
            else:
                budget = _p2_wave_budget(
                    selection_diag, total_rates, type_rates, supplement.safety_factor
                )
                weights = _p2_routing_weights(
                    selection_diag, total_rates, type_rates, supplement.exploration_share
                )
            used = 0
            requested_total = 0
            batch_id = 0
            batch_summaries: list[JsonObject] = []
            wave_exhausted = False
            while used < budget:
                if selection_diag["target_gap"] == 0 and not any(
                    selection_diag["soft_min_deficit_by_primary"].values()
                ):
                    break
                if wave > 0:
                    total_rates, type_rates = source_yield_rates(
                        sampled_metadata,
                        challenge_results,
                        default_yield=supplement.default_yield,
                        type_default_yield=supplement.type_default_yield,
                        prior_strength=supplement.prior_strength,
                        minimum_yield=supplement.minimum_yield,
                    )
                    weights = _p2_routing_weights(
                        selection_diag, total_rates, type_rates, supplement.exploration_share
                    )
                in_tail = (
                    selection_diag["target_gap"] == 0
                    and any(selection_diag["soft_min_deficit_by_primary"].values())
                )
                total_remaining = supplement.max_sampled - total_sampled
                tail_remaining = supplement.max_tail_sampled - tail_sampled
                if total_remaining <= 0:
                    budget_stop_reason = "max_sampled"
                    break
                if in_tail and tail_remaining <= 0:
                    budget_stop_reason = "max_tail_sampled"
                    break
                if in_tail and stalled_batches >= supplement.max_stalled_batches:
                    budget_stop_reason = "stalled_batches"
                    break
                request_n = min(
                    supplement.tail_batch_size if in_tail else p2_config.batch_size,
                    budget - used,
                    total_remaining,
                    tail_remaining if in_tail else total_remaining,
                )
                deficits_before = sum(
                    int(value)
                    for value in selection_diag["soft_min_deficit_by_primary"].values()
                )
                if initial_capacity_pending and not processed:
                    current_capacities = dict(source_capacity)
                    initial_capacity_pending = False
                else:
                    current_capacities = sampler.capacities(processed)
                requested = allocate_by_weights(
                    request_n, weights, capacities=current_capacities
                )
                if not any(requested.values()):
                    wave_exhausted = True
                    break
                batch_id += 1
                plan_name = f"p2_wave_{wave}_batch_{batch_id}_plan"
                result_name = f"p2_wave_{wave}_batch_{batch_id}_result"
                stage1_name = f"p2_wave_{wave}_batch_{batch_id}_stage1"
                challenge_name = f"p2_wave_{wave}_batch_{batch_id}_challenge"
                plan = checkpoints.load(plan_name)
                if plan is None:
                    planning_started = time.monotonic()
                    draw = sampler.draw(requested, processed)
                    raw = _materialize(index, source_path, taxonomy, draw.locators, materialized)
                    batch_dedup: list[JsonObject] = []
                    new_candidates = _deduplicate(
                        "P2", raw, evaluated, config=config.sampling.dedup,
                        idf_snapshot=idf_snapshot, audit=batch_dedup,
                    )
                    new_ids = {str(item["source_identity_sha256"]) for item in new_candidates}
                    plan = {
                        "wave": wave,
                        "batch": batch_id,
                        "requested_by_source": requested,
                        "available_by_source": dict(draw.available_by_source),
                        "sampled_identities": [item["source_identity_sha256"] for item in draw.locators],
                        "sampled_metadata": dict(draw.metadata),
                        "candidates": new_candidates,
                        "dedup_audit": batch_dedup,
                        "metadata": {
                            identity: value for identity, value in draw.metadata.items()
                            if identity in new_ids
                        },
                        "planning_duration_ms": round(
                            (time.monotonic() - planning_started) * 1000
                        ),
                    }
                    checkpoints.save(plan_name, plan)
                elif plan["requested_by_source"] != requested:
                    raise ValueError(f"P2 第 {wave} 轮第 {batch_id} 批计划与续跑状态不一致")

                sampled_ids = {str(value) for value in plan["sampled_identities"]}
                dedup_events.extend(plan["dedup_audit"])
                processed.update(sampled_ids)
                new_candidates = list(plan["candidates"])
                new_sampled_metadata = {
                    str(key): dict(value)
                    for key, value in plan["sampled_metadata"].items()
                }
                new_metadata = {str(key): dict(value) for key, value in plan["metadata"].items()}
                _merge_candidate_tasks(
                    branch_root / "candidate_tasks.jsonl",
                    [
                        {
                            **_candidate_task(candidate, "P2", wave),
                            **new_metadata[candidate["source_identity_sha256"]],
                        }
                        for candidate in new_candidates
                    ],
                )
                result = checkpoints.load(result_name)
                if result is None:
                    saved_stage1 = checkpoints.load(stage1_name)
                    if saved_stage1 is None:
                        stage1_started = time.monotonic()
                        batch_stage1 = (
                            _run_quality(quality_runner, "run_stage1", new_candidates, "P2")
                            if new_candidates else {}
                        )
                        stage1_duration_ms = round((time.monotonic() - stage1_started) * 1000)
                        checkpoints.save(stage1_name, {
                            "results": batch_stage1,
                            "duration_ms": stage1_duration_ms,
                        })
                    else:
                        batch_stage1 = {
                            str(key): dict(value)
                            for key, value in saved_stage1["results"].items()
                        }
                        stage1_duration_ms = int(saved_stage1.get("duration_ms", 0))
                    stage1_passed = _passing(new_candidates, batch_stage1) if new_candidates else []
                    saved_challenge = checkpoints.load(challenge_name)
                    if saved_challenge is None:
                        challenge_started = time.monotonic()
                        batch_challenge = (
                            _run_quality(quality_runner, "run_challenge", stage1_passed, "P2")
                            if stage1_passed else {}
                        )
                        challenge_duration_ms = round(
                            (time.monotonic() - challenge_started) * 1000
                        )
                        checkpoints.save(challenge_name, {
                            "results": batch_challenge,
                            "duration_ms": challenge_duration_ms,
                        })
                    else:
                        batch_challenge = {
                            str(key): dict(value)
                            for key, value in saved_challenge["results"].items()
                        }
                        challenge_duration_ms = int(saved_challenge.get("duration_ms", 0))
                    result = {
                        "stage1_results": batch_stage1,
                        "challenge_results": batch_challenge,
                        "stage1_duration_ms": stage1_duration_ms,
                        "challenge_duration_ms": challenge_duration_ms,
                    }
                    checkpoints.save(result_name, result)
                batch_stage1 = {str(key): dict(value) for key, value in result["stage1_results"].items()}
                batch_challenge = {str(key): dict(value) for key, value in result["challenge_results"].items()}
                stage1_results.update(batch_stage1)
                challenge_results.update(batch_challenge)
                sampled_metadata.update(new_sampled_metadata)
                metadata.update(new_metadata)
                evaluated.extend(new_candidates)
                evaluation_rounds.update({
                    str(candidate["source_identity_sha256"]): wave for candidate in new_candidates
                })
                stage1_passed = _passing(new_candidates, batch_stage1) if new_candidates else []
                challenge_passed = sum(
                    challenge_passes(batch_challenge[item["source_identity_sha256"]])
                    for item in stage1_passed
                )
                sampled_by_source = Counter(
                    str(value["sampled_from"]) for value in new_sampled_metadata.values()
                )
                after_dedup_by_source = Counter(
                    str(value["sampled_from"]) for value in new_metadata.values()
                )
                stage1_by_source = Counter(
                    new_metadata[item["source_identity_sha256"]]["sampled_from"]
                    for item in stage1_passed
                )
                challenge_by_source = Counter(
                    new_metadata[item["source_identity_sha256"]]["sampled_from"]
                    for item in stage1_passed
                    if challenge_passes(batch_challenge[item["source_identity_sha256"]])
                )
                primary_by_source: dict[str, Counter[str]] = {
                    source: Counter() for source in P2_SOURCE_NAMES
                }
                for item in stage1_passed:
                    identity = item["source_identity_sha256"]
                    result_value = batch_challenge[identity]
                    if challenge_passes(result_value):
                        primary_by_source[new_metadata[identity]["sampled_from"]][
                            result_value["primary_challenge"]
                        ] += 1
                selection_started = time.monotonic()
                selected, selection_diag = select_final_p2(
                    evaluated, challenge_results, p2_config
                )
                selection_duration_ms = round(
                    (time.monotonic() - selection_started) * 1000
                )
                requested_total += sum(requested.values())
                # Overlapping source pools can make a requested slot unavailable
                # after an earlier source claims the same identity. Count actual
                # draws against the wave budget so the next batch can refill it.
                used += len(sampled_ids)
                total_sampled += len(sampled_ids)
                processed_batches += 1
                if in_tail:
                    tail_sampled += len(sampled_ids)
                    deficits_after = sum(
                        int(value)
                        for value in selection_diag["soft_min_deficit_by_primary"].values()
                    )
                    stalled_batches = _updated_p2_stalled_batches(
                        deficits_before, deficits_after, stalled_batches
                    )
                else:
                    stalled_batches = 0
                next_action = _p2_next_action(
                    selection_diag,
                    sampled=bool(sampled_ids),
                    wave=wave,
                    wave_sampled=used,
                    wave_budget=budget,
                    total_sampled=total_sampled,
                    tail_sampled=tail_sampled,
                    stalled_batches=stalled_batches,
                    supplement=supplement,
                )
                batch_summary = {
                    "batch": batch_id,
                    "mode": "coverage_tail" if in_tail else "target_fill",
                    "requested": sum(requested.values()),
                    "requested_by_source": requested,
                    "sampled": len(sampled_ids),
                    "sampled_by_source": dict(sampled_by_source),
                    "after_dedup": len(new_candidates),
                    "after_dedup_by_source": dict(after_dedup_by_source),
                    "stage1_pass": len(stage1_passed),
                    "stage1_pass_by_source": dict(stage1_by_source),
                    "judge_pass": challenge_passed,
                    "challenge_pass": challenge_passed,
                    "judge_pass_by_source": dict(challenge_by_source),
                    "final": len(selected),
                    "target": p2_config.target_n,
                    "target_gap": selection_diag["target_gap"],
                    "selected_by_primary": dict(selection_diag["selected_by_primary"]),
                    "deficit_by_primary": dict(
                        selection_diag["soft_min_deficit_by_primary"]
                    ),
                    "sampled_budget": {
                        "used": total_sampled,
                        "limit": supplement.max_sampled,
                    },
                    "tail_budget": {
                        "used": tail_sampled,
                        "limit": supplement.max_tail_sampled,
                    },
                    "stalled_batches": {
                        "used": stalled_batches,
                        "limit": supplement.max_stalled_batches,
                    },
                    "next_action": next_action,
                    "phase_duration_ms": {
                        "planning": int(plan.get("planning_duration_ms", 0)),
                        "stage1": int(result.get("stage1_duration_ms", 0)),
                        "challenge": int(result.get("challenge_duration_ms", 0)),
                        "selection": selection_duration_ms,
                    },
                    "total_yield": 0.0 if not sampled_ids else challenge_passed / len(sampled_ids),
                    "source_yield": {
                        source: (
                            0.0 if sampled_by_source[source] == 0
                            else challenge_by_source[source] / sampled_by_source[source]
                        ) for source in P2_SOURCE_NAMES
                    },
                    "source_primary_yield": {
                        source: {
                            challenge_type: (
                                0.0 if sampled_by_source[source] == 0
                                else primary_by_source[source][challenge_type] / sampled_by_source[source]
                            )
                            for challenge_type in CHALLENGE_TYPES
                        }
                        for source in P2_SOURCE_NAMES
                    },
                }
                batch_summaries.append(batch_summary)
                progress = {
                    "event": "p2_batch_progress",
                    "wave": wave,
                    **{
                        key: batch_summary[key]
                        for key in (
                            "batch", "mode", "requested", "sampled", "stage1_pass",
                            "challenge_pass", "final", "target", "target_gap",
                            "selected_by_primary", "deficit_by_primary",
                            "sampled_budget", "tail_budget", "stalled_batches",
                            "next_action",
                        )
                    },
                }
                logger.info(
                    "P2 batch_progress %s",
                    json.dumps(progress, ensure_ascii=False, sort_keys=True),
                )
                budget_state = {
                    "total_sampled": total_sampled,
                    "tail_sampled": tail_sampled,
                    "stalled_batches": stalled_batches,
                    "processed_batches": processed_batches,
                }
                frozen_batches = (
                    int(frozen_budget_state["processed_batches"])
                    if frozen_budget_state is not None else 0
                )
                if frozen_budget_state is not None and processed_batches == frozen_batches:
                    if budget_state != frozen_budget_state:
                        raise ValueError("P2 预算检查点与已完成批次不一致")
                if frozen_budget_state is None or processed_batches >= frozen_batches:
                    checkpoints.save("p2_budget_state", budget_state)
                if not sampled_ids:
                    wave_exhausted = True
                    break
            target_met = selection_diag["target_gap"] == 0 and not any(
                selection_diag["soft_min_deficit_by_primary"].values()
            )
            rounds.append({
                "round": wave,
                "planned_requests": budget,
                "used_requests": requested_total,
                "actual_sampled": used,
                "batches": batch_summaries,
                "final_count_after_round": len(selected),
                "target_gap_after_round": selection_diag["target_gap"],
                "type_deficits_after_round": selection_diag["soft_min_deficit_by_primary"],
                "stop": (
                    "target_and_soft_mins_met" if target_met else
                    "candidate_exhausted" if wave_exhausted else
                    "round_requests_used"
                ),
            })
            logger.info(
                "P2 wave=%d complete batches=%d final=%d target_gap=%d",
                wave,
                len(batch_summaries),
                len(selected),
                selection_diag["target_gap"],
            )
            if wave_exhausted:
                exhausted = True
                break
            if budget_stop_reason is not None:
                break
        source_remaining = sampler.capacities(processed)
        exhausted = exhausted or not any(source_remaining.values())

    selected, selection_diag = select_final_p2(evaluated, challenge_results, p2_config)
    selection_rounds = {
        str(candidate["source_identity_sha256"]): evaluation_rounds[str(candidate["source_identity_sha256"])]
        for candidate in selected
    }
    if selection_diag["target_gap"] == 0 and not any(selection_diag["soft_min_deficit_by_primary"].values()):
        stop_reason = "target_and_soft_mins_met"
    elif budget_stop_reason is not None:
        stop_reason = budget_stop_reason
    elif exhausted:
        stop_reason = "candidate_exhausted"
    else:
        stop_reason = "max_rounds"

    source_funnel: dict[str, JsonObject] = {}
    final_ids = {str(item["source_identity_sha256"]) for item in selected}
    for source in P2_SOURCE_NAMES:
        sampled_source_ids = {
            identity
            for identity, value in sampled_metadata.items()
            if value["sampled_from"] == source
        }
        source_ids = {identity for identity, value in metadata.items() if value["sampled_from"] == source}
        stage1_ids = {identity for identity in source_ids if stage1_passes(stage1_results[identity])}
        judge_ids = {
            identity for identity in stage1_ids
            if identity in challenge_results and challenge_passes(challenge_results[identity])
        }
        source_funnel[source] = {
            "candidate_capacity": source_capacity[source],
            "requested": sum(
                int(batch["requested_by_source"].get(source, 0))
                for value in rounds for batch in value["batches"]
            ),
            "actual_sampled": len(sampled_source_ids),
            "after_dedup": len(source_ids),
            "stage1_pass": len(stage1_ids),
            "judge_pass": len(judge_ids),
            "final": len(source_ids & final_ids),
        }
    primary_counts = Counter(
        str(challenge_results[identity]["primary_challenge"])
        for identity in final_ids
    )
    combinations = Counter(
        "+".join(challenge_results[identity]["challenge_types"])
        for identity in final_ids
    )
    phase_duration_ms = {
        phase: sum(
            int(batch.get("phase_duration_ms", {}).get(phase, 0))
            for round_value in rounds
            for batch in round_value["batches"]
        )
        for phase in ("planning", "stage1", "challenge", "selection")
    }
    diagnostic = {
        "algorithm": P2_ALGORITHM,
        "rounds": rounds,
        "source_funnel": source_funnel,
        "candidate_remaining_by_source": source_remaining,
        "selection": selection_diag,
        "final_by_primary": {key: primary_counts[key] for key in CHALLENGE_TYPES},
        "final_share_by_primary": {
            key: (0.0 if not selected else round(primary_counts[key] / len(selected), 6))
            for key in CHALLENGE_TYPES
        },
        "challenge_type_combinations": dict(sorted(combinations.items(), key=lambda item: (-item[1], item[0]))),
        "phase_duration_ms": phase_duration_ms,
        "stop_reason": stop_reason,
        "evaluated": len(evaluated),
        "stage1_pass": sum(stage1_passes(value) for value in stage1_results.values()),
        "judge_pass": sum(challenge_passes(value) for value in challenge_results.values()),
        "final": len(selected),
        "target": p2_config.target_n,
        "soft_min_by_primary": dict(p2_config.soft_min_by_primary),
        "max_per_primary": p2_config.max_per_primary,
        "target_gap": selection_diag["target_gap"],
        "candidate_exhausted": exhausted,
        "budget": {
            "max_sampled": supplement.max_sampled,
            "sampled": total_sampled,
            "remaining": max(0, supplement.max_sampled - total_sampled),
            "max_tail_sampled": supplement.max_tail_sampled,
            "tail_sampled": tail_sampled,
            "tail_remaining": max(0, supplement.max_tail_sampled - tail_sampled),
            "tail_batch_size": supplement.tail_batch_size,
            "max_stalled_batches": supplement.max_stalled_batches,
            "stalled_batches": stalled_batches,
        },
    }
    result: JsonObject = {
        "selected": selected,
        "evaluated_candidates": evaluated,
        "stage1_results": stage1_results,
        "challenge_results": challenge_results,
        "metadata": metadata,
        "evaluation_rounds": evaluation_rounds,
        "selection_rounds": selection_rounds,
        "diagnostics": diagnostic,
        "dedup_audit": dedup_events,
    }
    quality_root = getattr(quality_runner, "run_root", None)
    if isinstance(quality_root, Path):
        for filename in ("challenge_tasks.jsonl", "challenge_results.jsonl", "challenge_attempts.jsonl"):
            path = quality_root / filename
            if not path.exists():
                path.write_text("", encoding="utf-8", newline="\n")
    write_json(branch_root / "adaptive_summary.json", diagnostic)
    checkpoints.save("p2_adaptive_complete", result)
    logger.info(
        "P2 adaptive_complete %s",
        json.dumps({
            "event": "p2_adaptive_complete",
            "final": len(selected),
            "target": p2_config.target_n,
            "target_gap": selection_diag["target_gap"],
            "selected_by_primary": dict(selection_diag["selected_by_primary"]),
            "deficit_by_primary": dict(
                selection_diag["soft_min_deficit_by_primary"]
            ),
            "sampled_budget": {
                "used": total_sampled,
                "limit": supplement.max_sampled,
            },
            "tail_budget": {
                "used": tail_sampled,
                "limit": supplement.max_tail_sampled,
            },
            "stalled_batches": {
                "used": stalled_batches,
                "limit": supplement.max_stalled_batches,
            },
            "stop_reason": stop_reason,
        }, ensure_ascii=False, sort_keys=True),
    )
    return result


def _branch_contract(
    set_name: str,
    index: CanonicalIndexHandle,
    config: BuildConfig,
    idf_snapshot: Mapping[str, Any],
) -> JsonObject:
    return {
        "build_config_version": BUILD_CONFIG_VERSION,
        "set": set_name,
        "config": asdict(config),
        "source_snapshot_id": index.metadata.get("source_snapshot_id"),
        "taxonomy_sha256": index.metadata.get("taxonomy_sha256"),
        "dedup_rules_sha256": RULES_SHA256,
        "dedup_idf_sha256": idf_snapshot["content_sha256"],
    }


def _set_branch_status(run_root: Path, set_name: str, status: str, **details: Any) -> None:
    manifest_path = run_root / "manifest.json"
    if not manifest_path.is_file():
        return
    from .run_context import update_manifest

    payload = {"status": status, **details}
    payload.setdefault("error", None)
    update_manifest(run_root, branches={set_name: payload})


def _restore_completed_branch(
    set_name: str,
    branch_root: Path,
    checkpoints: CheckpointStore,
    run_root: Path,
) -> JsonObject | None:
    complete = checkpoints.load("branch_complete")
    if complete is None:
        return None
    records = tuple(complete["records"])
    validate_screened_canonical_records(set_name, records)
    artifacts = complete.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError(f"{set_name} 完成检查点缺少产物快照")
    for relative_path, expected_hash in artifacts.items():
        path = branch_root / str(relative_path)
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"{set_name} 完成检查点产物缺失或已变化: {relative_path}")
    manifest_path = run_root / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file() else {}
    )
    prior = manifest.get("branches", {}).get(set_name, {})
    status = "published" if prior.get("status") == "published" else "completed"
    _set_branch_status(
        run_root, set_name, status, restored=True, records=len(records),
        stage=status, artifacts=dict(artifacts),
    )
    return complete


def _finalize_branch(
    set_name: str,
    *,
    run_root: Path,
    checkpoints: CheckpointStore,
    quality_runner: TwoStageQualityRunner,
    selected: Sequence[JsonObject],
    evaluated: Sequence[JsonObject],
    stage1: Mapping[str, JsonObject],
    evaluation_rounds: Mapping[str, int],
    selection_rounds: Mapping[str, int],
    dedup_audit: Sequence[JsonObject],
    audit: JsonObject,
    challenge_results: Mapping[str, JsonObject] | None = None,
    p2_metadata: Mapping[str, JsonObject] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JsonObject:
    branch_root = run_root / set_name
    complete = _restore_completed_branch(set_name, branch_root, checkpoints, run_root)
    if complete is not None:
        return complete

    p2_metadata = p2_metadata or {}
    challenge_results = challenge_results or {}
    _merge_candidate_tasks(
        branch_root / "candidate_tasks.jsonl",
        [
            {
                **_candidate_task(candidate, set_name, int(evaluation_rounds[candidate["source_identity_sha256"]])),
                **(p2_metadata[candidate["source_identity_sha256"]] if set_name == "P2" else {}),
            }
            for candidate in evaluated
        ],
    )
    write_jsonl(branch_root / "dedup_audit.jsonl", dedup_audit)

    stage2_checkpoint = checkpoints.load("stage2")
    if stage2_checkpoint is None:
        _set_branch_status(run_root, set_name, "running", stage="stage2")
        selected_stage1 = {
            candidate["source_identity_sha256"]: stage1[candidate["source_identity_sha256"]]
            for candidate in selected
        }
        stage2 = _run_quality(
            quality_runner, "run_stage2", selected, set_name, selected_stage1
        ) if selected else {}
        checkpoints.save("stage2", {"results": stage2})
    else:
        stage2 = {str(key): dict(value) for key, value in stage2_checkpoint["results"].items()}

    risk_snapshot = json.loads((run_root / "risk_keywords.json").read_text(encoding="utf-8"))
    records = tuple(
        build_screened_canonical_record(
            set_name,
            candidate,
            stage1[candidate["source_identity_sha256"]],
            stage2[candidate["source_identity_sha256"]],
            delivery_labels(candidate, stage2[candidate["source_identity_sha256"]], risk_snapshot),
            challenge_result=(challenge_results[candidate["source_identity_sha256"]] if set_name == "P2" else None),
            challenge_task_id=(
                _quality_task_id(quality_runner, "challenge", candidate)
                if set_name == "P2" else None
            ),
            origin=(
                "initial_wave"
                if int(selection_rounds[candidate["source_identity_sha256"]]) == 0
                else "supplement_wave"
            ),
            round_id=int(selection_rounds[candidate["source_identity_sha256"]]),
            candidate_task_id=(
                f"{set_name}:{selection_rounds[candidate['source_identity_sha256']]}:"
                f"{candidate['source_identity_sha256']}"
            ),
            stage1_task_id=_quality_task_id(quality_runner, "stage1", candidate),
            stage2_task_id=_quality_task_id(
                quality_runner, "stage2", candidate,
                stage1[candidate["source_identity_sha256"]],
            ),
        )
        for candidate in selected
    )
    tasks = {
        str(task["task_id"]): task
        for task in load_jsonl(branch_root / "candidate_tasks.jsonl")
    }
    validate_screened_canonical_records(
        set_name, records, expected_candidate_tasks=tasks, expected_final_count=len(selected)
    )
    _set_branch_status(run_root, set_name, "running", stage="export")
    export_report = export_legacy_branch(set_name, records, branch_root)
    final_audit = {**audit, "final": len(records)}
    write_json(branch_root / "branch_audit.json", final_audit)
    write_json(
        branch_root / "branch_report.json",
        {
            "report_schema_version": RUN_REPORT_SCHEMA_VERSION,
            "set": set_name,
            "status": "completed",
            "evaluated": len(evaluated),
            "final": len(records),
            "audit": final_audit,
            "export": export_report,
        },
    )
    artifact_paths = (
        "screened.canonical.jsonl",
        export_report["files"]["legacy"],
        "export_provenance.jsonl",
        "legacy_export_report.json",
        "branch_audit.json",
        "branch_report.json",
    )
    artifacts = {
        name: sha256_file(branch_root / name)
        for name in artifact_paths
    }
    payload: JsonObject = {
        "set": set_name,
        "records": list(records),
        "selected": list(selected),
        "evaluated_candidates": list(evaluated),
        "stage1_results": dict(stage1),
        "evaluation_rounds": dict(evaluation_rounds),
        "selection_rounds": dict(selection_rounds),
        "dedup_audit": list(dedup_audit),
        "audit": final_audit,
        "challenge_results": dict(challenge_results),
        "p2_metadata": dict(p2_metadata),
        "export_report": export_report,
        "artifacts": artifacts,
        **dict(extra or {}),
    }
    checkpoints.save("branch_complete", payload)
    _set_branch_status(
        run_root, set_name, "completed", records=len(records), stage="completed",
        artifacts=artifacts,
    )
    return payload


def _run_p0_branch(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
    idf_snapshot: Mapping[str, Any],
) -> JsonObject:
    set_name = "P0"
    branch_root = run_root / set_name
    branch_root.mkdir(parents=True, exist_ok=True)
    checkpoints = CheckpointStore(branch_root, _branch_contract(set_name, index, config, idf_snapshot))
    complete = _restore_completed_branch(set_name, branch_root, checkpoints, run_root)
    if complete is not None:
        return complete
    _set_branch_status(run_root, set_name, "running", stage="sampling")
    materialized: dict[str, JsonObject] = {}
    initial = checkpoints.load("initial")
    if initial is None:
        locators = _sample_branch(set_name, index, source_path, taxonomy, config.sampling)
        raw = _materialize(index, source_path, taxonomy, locators, materialized)
        dedup_audit: list[JsonObject] = []
        candidates = _deduplicate(
            set_name, raw, config=config.sampling.dedup,
            idf_snapshot=idf_snapshot, audit=dedup_audit,
        )
        sampled_ids = [item["source_identity_sha256"] for item in locators]
        target_by_cluster = Counter(item["cluster_id"] for item in locators)
        backfill_gap = 0
        while True:
            kept = Counter(item["taxonomy"]["cluster_id"] for item in candidates)
            deficits = {key: value - kept[key] for key, value in target_by_cluster.items() if value > kept[key]}
            if not deficits:
                break
            replacements = _sample_branch(
                set_name, index, source_path, taxonomy, config.sampling,
                sampled_ids, deficits,
            )
            if not replacements:
                backfill_gap = sum(deficits.values())
                break
            sampled_ids.extend(item["source_identity_sha256"] for item in replacements)
            additions = _deduplicate(
                set_name,
                _materialize(index, source_path, taxonomy, replacements, materialized),
                candidates,
                config=config.sampling.dedup,
                idf_snapshot=idf_snapshot,
                audit=dedup_audit,
            )
            candidates.extend(additions)
        initial = {
            "candidates": candidates, "sampled_identities": sampled_ids,
            "dedup_audit": dedup_audit, "backfill_gap": backfill_gap,
        }
        checkpoints.save("initial", initial)
    candidates = list(initial["candidates"])
    _merge_candidate_tasks(
        branch_root / "candidate_tasks.jsonl",
        [_candidate_task(candidate, set_name, 0) for candidate in candidates],
    )
    stage1_checkpoint = checkpoints.load("stage1")
    if stage1_checkpoint is None:
        _set_branch_status(run_root, set_name, "running", stage="stage1")
        stage1 = _run_quality(quality_runner, "run_stage1", candidates, set_name)
        checkpoints.save("stage1", {"results": stage1})
    else:
        stage1 = dict(stage1_checkpoint["results"])
    selected = _passing(candidates, stage1)
    audit = _branch_audit(
        set_name, len(initial["sampled_identities"]), len(candidates), candidates, stage1
    )
    audit.update({
        "supplement_sampled": 0, "supplement_after_dedup": 0,
        "supplement_stage1_pass": 0, "supplement_admitted": 0,
        "backfill_gap": int(initial["backfill_gap"]),
    })
    return _finalize_branch(
        set_name, run_root=run_root, checkpoints=checkpoints,
        quality_runner=quality_runner, selected=selected, evaluated=candidates,
        stage1=stage1,
        evaluation_rounds={item["source_identity_sha256"]: 0 for item in candidates},
        selection_rounds={item["source_identity_sha256"]: 0 for item in selected},
        dedup_audit=initial["dedup_audit"], audit=audit,
        extra={"backfill_gap": int(initial["backfill_gap"])},
    )


def _run_p1_branch(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
    idf_snapshot: Mapping[str, Any],
) -> JsonObject:
    set_name = "P1"
    branch_root = run_root / set_name
    branch_root.mkdir(parents=True, exist_ok=True)
    checkpoints = CheckpointStore(branch_root, _branch_contract(set_name, index, config, idf_snapshot))
    complete = _restore_completed_branch(set_name, branch_root, checkpoints, run_root)
    if complete is not None:
        return complete
    _set_branch_status(run_root, set_name, "running", stage="sampling")
    materialized: dict[str, JsonObject] = {}
    initial = checkpoints.load("initial")
    if initial is None:
        locators = _sample_branch(set_name, index, source_path, taxonomy, config.sampling)
        raw = _materialize(index, source_path, taxonomy, locators, materialized)
        dedup_audit: list[JsonObject] = []
        candidates = _deduplicate(
            set_name, raw, config=config.sampling.dedup,
            idf_snapshot=idf_snapshot, audit=dedup_audit,
        )
        initial = {
            "candidates": candidates,
            "sampled_identities": [item["source_identity_sha256"] for item in locators],
            "dedup_audit": dedup_audit,
        }
        checkpoints.save("initial", initial)
    initial_candidates = list(initial["candidates"])
    _merge_candidate_tasks(
        branch_root / "candidate_tasks.jsonl",
        [_candidate_task(candidate, set_name, 0) for candidate in initial_candidates],
    )
    initial_stage1_checkpoint = checkpoints.load("stage1_initial")
    if initial_stage1_checkpoint is None:
        _set_branch_status(run_root, set_name, "running", stage="stage1")
        stage1 = _run_quality(quality_runner, "run_stage1", initial_candidates, set_name)
        checkpoints.save("stage1_initial", {"results": stage1})
    else:
        stage1 = dict(initial_stage1_checkpoint["results"])

    quota_checkpoint = checkpoints.load("quotas")
    if quota_checkpoint is None:
        with CanonicalCandidateIndex(index) as readonly_index:
            capacities = readonly_index.available_counts("l2")
        quotas = allocate_sqrt_quotas(
            taxonomy.l2_population, config.sampling.p1.target_n, capacities=capacities
        )
        checkpoints.save("quotas", {"quotas": quotas})
    else:
        quotas = {str(key): int(value) for key, value in quota_checkpoint["quotas"].items()}
    selected: list[JsonObject] = []
    selected_counts: Counter[str] = Counter()
    for candidate in _passing(initial_candidates, stage1):
        key = candidate["taxonomy"]["l2_key"]
        if selected_counts[key] < quotas.get(key, 0):
            selected.append(candidate)
            selected_counts[key] += 1
    initial_pass_count = len(_passing(initial_candidates, stage1))
    initial_admitted_count = len(selected)
    evaluated = list(initial_candidates)
    evaluation_rounds = {item["source_identity_sha256"]: 0 for item in evaluated}
    selection_rounds = {item["source_identity_sha256"]: 0 for item in selected}
    dedup_audit = list(initial["dedup_audit"])
    processed = set(initial["sampled_identities"])
    initial_terminal = _l2_counts(initial_candidates)
    initial_pass = _l2_counts(selected)
    supplement_terminal: Counter[str] = Counter()
    supplement_pass: Counter[str] = Counter()
    feedback: dict[Any, list[int]] = {}
    _update_cluster_feedback(feedback, initial_candidates, stage1)
    with CanonicalCandidateIndex(index) as readonly_index:
        cluster_remaining = readonly_index.available_counts("cluster")
        for cluster, count in readonly_index.cluster_counts_for_identities(processed).items():
            cluster_remaining[cluster] -= count
    rounds: list[JsonObject] = []
    _set_branch_status(run_root, set_name, "running", stage="supplement")
    for round_id in range(1, config.p1_supplement.max_rounds + 1):
        current = _l2_counts(selected)
        deficits = {key: max(0, quota - current[key]) for key, quota in quotas.items()}
        if not any(deficits.values()):
            break
        rates = _pass_rates(
            list(quotas), initial_terminal, initial_pass,
            supplement_terminal, supplement_pass, config.p1_supplement,
        )
        requested = {
            key: math.ceil(value / rates[key] * config.p1_supplement.safety_factor)
            for key, value in deficits.items() if value > 0
        }
        used: Counter[str] = Counter()
        round_draws: Counter[Any] = Counter()
        round_cold: Counter[Any] = Counter()
        round_cluster_counts = {
            key: sum(
                cluster_remaining.get(cluster, 0) > 0
                for cluster in taxonomy.l2_clusters.get(key, ())
            )
            for key in requested
        }
        batch_summaries: list[JsonObject] = []
        batch_id = 0
        while True:
            current = _l2_counts(selected)
            deficits = {key: max(0, quota - current[key]) for key, quota in quotas.items()}
            batch_request = {
                key: min(value, requested[key] - used[key])
                for key, value in deficits.items()
                if value > 0 and used[key] < requested.get(key, 0)
                and any(cluster_remaining.get(cluster, 0) > 0 for cluster in taxonomy.l2_clusters.get(key, ()))
            }
            if not batch_request:
                break
            batch_id += 1
            plan_name = f"round_{round_id}_batch_{batch_id}_plan"
            plan = checkpoints.load(plan_name)
            if plan is None:
                with CanonicalCandidateIndex(index) as readonly_index:
                    sampler = CandidateSampler(readonly_index, source_path, taxonomy, config.sampling)
                    locators = sampler.sample_p1_supplement_batch(
                        batch_request, processed,
                        remaining_by_cluster=cluster_remaining,
                        evaluated_passed_by_cluster=feedback,
                        prior_by_l2=rates,
                        prior_strength=config.p1_supplement.pass_rate_prior_strength,
                        round_draws_by_cluster=round_draws,
                        round_cold_by_cluster=round_cold,
                        round_request_by_l2=requested,
                        round_cluster_count_by_l2=round_cluster_counts,
                    )
                if not locators:
                    break
                new_candidates = _deduplicate(
                    set_name,
                    _materialize(index, source_path, taxonomy, locators, materialized),
                    evaluated,
                    config=config.sampling.dedup,
                    idf_snapshot=idf_snapshot,
                    audit=(batch_audit := []),
                )
                plan = {
                    "sampled_identities": [item["source_identity_sha256"] for item in locators],
                    "sampled_by_l2": dict(Counter(item["l2_key"] for item in locators)),
                    "sampled_by_cluster": [
                        {"cluster_id": cluster, "count": count}
                        for cluster, count in Counter(item["cluster_id"] for item in locators).items()
                    ],
                    "cold_by_cluster": [
                        {"cluster_id": cluster, "count": count}
                        for cluster, count in Counter(
                            item["cluster_id"] for item in locators
                            if item["features"]["probability_band"] == "boundary"
                            and item["features"]["frequency_band"] == "cold"
                        ).items()
                    ],
                    "candidates": new_candidates,
                    "dedup_audit": batch_audit,
                }
                checkpoints.save(plan_name, plan)
            processed.update(str(value) for value in plan["sampled_identities"])
            used.update({str(key): int(value) for key, value in plan["sampled_by_l2"].items()})
            for row in plan["sampled_by_cluster"]:
                cluster_remaining[row["cluster_id"]] -= int(row["count"])
                round_draws[row["cluster_id"]] += int(row["count"])
            for row in plan.get("cold_by_cluster", []):
                round_cold[row["cluster_id"]] += int(row["count"])
            dedup_audit.extend(plan["dedup_audit"])
            new_candidates = list(plan["candidates"])
            evaluated.extend(new_candidates)
            evaluation_rounds.update({item["source_identity_sha256"]: round_id for item in new_candidates})
            _merge_candidate_tasks(
                branch_root / "candidate_tasks.jsonl",
                [_candidate_task(candidate, set_name, round_id) for candidate in new_candidates],
            )
            result_name = f"round_{round_id}_batch_{batch_id}_result"
            result = checkpoints.load(result_name)
            if result is None:
                batch_stage1 = (
                    _run_quality(quality_runner, "run_stage1", new_candidates, set_name)
                    if new_candidates else {}
                )
                passed = _passing(new_candidates, batch_stage1) if new_candidates else []
                result = {
                    "stage1_results": batch_stage1,
                    "admitted_identities": [item["source_identity_sha256"] for item in passed],
                }
                checkpoints.save(result_name, result)
            batch_stage1 = {str(key): dict(value) for key, value in result["stage1_results"].items()}
            stage1.update(batch_stage1)
            passed = _passing(new_candidates, batch_stage1) if new_candidates else []
            selected.extend(passed)
            selection_rounds.update({item["source_identity_sha256"]: round_id for item in passed})
            supplement_terminal.update(_l2_counts(new_candidates))
            supplement_pass.update(_l2_counts(passed))
            _update_cluster_feedback(feedback, new_candidates, batch_stage1)
            batch_summaries.append({
                "batch": batch_id, "sampled": len(plan["sampled_identities"]),
                "after_dedup": len(new_candidates), "stage1_pass": len(passed),
                "admitted": len(passed), "l2": {}, "clusters": [],
            })
        remaining = {key: max(0, quota - _l2_counts(selected)[key]) for key, quota in quotas.items()}
        round_stop = "quota_filled" if not any(remaining.values()) else "round_requests_used"
        rounds.append({
            "round": round_id, "requested_by_l2": requested,
            "estimated_pass_rate_by_l2": rates,
            "sampled": sum(item["sampled"] for item in batch_summaries),
            "after_dedup": sum(item["after_dedup"] for item in batch_summaries),
            "stage1_pass": sum(item["stage1_pass"] for item in batch_summaries),
            "admitted": sum(item["admitted"] for item in batch_summaries),
            "batches": batch_summaries, "stop": round_stop,
        })
        checkpoints.save(f"round_{round_id}_summary", rounds[-1])
        if round_stop == "quota_filled":
            break
    remaining_by_l2 = {
        key: sum(cluster_remaining.get(cluster, 0) for cluster in taxonomy.l2_clusters.get(key, ()))
        for key in quotas
    }
    final_counts = _l2_counts(selected)
    deficits = {key: max(0, quota - final_counts[key]) for key, quota in quotas.items()}
    unallocated = max(0, config.sampling.p1.target_n - sum(quotas.values()))
    stop_reason = (
        "quota_filled" if not any(deficits.values()) and not unallocated
        else "max_rounds" if len(rounds) >= config.p1_supplement.max_rounds
        else "candidate_exhausted"
    )
    supplement_summary: JsonObject = {
        "enabled": True, "target": config.sampling.p1.target_n,
        "target_quotas_by_l2": quotas, "rounds": rounds,
        "stop_reason": stop_reason, "final_count": len(selected),
        "unallocated_target": unallocated,
        "total_deficit": config.sampling.p1.target_n - len(selected),
        "deficits_by_l2": deficits,
        "candidate_remaining_by_l2": remaining_by_l2,
        "stop_reason_by_l2": {
            key: "quota_filled" if deficits[key] == 0 else (
                "candidate_exhausted" if remaining_by_l2[key] == 0 else "max_rounds"
            ) for key in quotas
        },
    }
    write_json(branch_root / "supplement_summary.json", supplement_summary)
    audit = _branch_audit(
        set_name, len(initial["sampled_identities"]), len(evaluated), evaluated, stage1
    )
    audit.update({
        "initial_stage1_pass": initial_pass_count,
        "initial_admitted": initial_admitted_count,
        "initial_pass_not_admitted": initial_pass_count - initial_admitted_count,
        "supplement_sampled": sum(item["sampled"] for item in rounds),
        "supplement_after_dedup": sum(item["after_dedup"] for item in rounds),
        "supplement_stage1_pass": sum(item["stage1_pass"] for item in rounds),
        "supplement_admitted": sum(item["admitted"] for item in rounds),
        "supplement_pass_not_admitted": 0,
        "target": config.sampling.p1.target_n,
        "deficit": config.sampling.p1.target_n - len(selected),
        "target_quota_sum": sum(quotas.values()),
        "unallocated_target": unallocated,
    })
    return _finalize_branch(
        set_name, run_root=run_root, checkpoints=checkpoints,
        quality_runner=quality_runner, selected=selected, evaluated=evaluated,
        stage1=stage1, evaluation_rounds=evaluation_rounds,
        selection_rounds=selection_rounds, dedup_audit=dedup_audit, audit=audit,
        extra={"p1_target_quotas": quotas, "p1_supplement": supplement_summary},
    )


def _run_p2_branch(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
    idf_snapshot: Mapping[str, Any],
) -> JsonObject:
    set_name = "P2"
    branch_root = run_root / set_name
    branch_root.mkdir(parents=True, exist_ok=True)
    checkpoints = CheckpointStore(branch_root, _branch_contract(set_name, index, config, idf_snapshot))
    complete = _restore_completed_branch(set_name, branch_root, checkpoints, run_root)
    if complete is not None:
        return complete
    _set_branch_status(run_root, set_name, "running", stage="adaptive")
    state = _run_adaptive_p2(
        index, config, source_path=source_path, taxonomy=taxonomy,
        run_root=run_root, quality_runner=quality_runner, checkpoints=checkpoints,
        materialized={}, logger=logging.getLogger("screening.v3.P2"),
        idf_snapshot=idf_snapshot,
    )
    diagnostics = dict(state["diagnostics"])
    audit = _branch_audit(
        set_name,
        sum(batch["sampled"] for wave in diagnostics["rounds"] for batch in wave["batches"]),
        len(state["evaluated_candidates"]), state["evaluated_candidates"], state["stage1_results"],
    )
    audit.update({
        "supplement_sampled": sum(
            batch["sampled"] for wave in diagnostics["rounds"][1:] for batch in wave["batches"]
        ),
        "supplement_after_dedup": sum(
            batch["after_dedup"] for wave in diagnostics["rounds"][1:] for batch in wave["batches"]
        ),
        "supplement_stage1_pass": sum(
            batch["stage1_pass"] for wave in diagnostics["rounds"][1:] for batch in wave["batches"]
        ),
        "supplement_admitted": sum(
            batch["judge_pass"] for wave in diagnostics["rounds"][1:] for batch in wave["batches"]
        ),
        "judge_pass": diagnostics["judge_pass"],
        "judge_pass_not_admitted": diagnostics["selection"]["judge_pass_not_selected"],
    })
    return _finalize_branch(
        set_name, run_root=run_root, checkpoints=checkpoints,
        quality_runner=quality_runner, selected=state["selected"],
        evaluated=state["evaluated_candidates"], stage1=state["stage1_results"],
        evaluation_rounds=state["evaluation_rounds"], selection_rounds=state["selection_rounds"],
        dedup_audit=state["dedup_audit"], audit=audit,
        challenge_results=state["challenge_results"], p2_metadata=state["metadata"],
        extra={"p2_diagnostics": diagnostics},
    )


def _aggregate_concurrent_results(
    config: BuildConfig,
    results: Mapping[str, JsonObject],
) -> JsonObject:
    records = {name: tuple(results[name]["records"]) for name in results}
    identities = {
        name: {row["candidate"]["source_identity_sha256"] for row in values}
        for name, values in records.items()
    }
    overlap = {
        "P0_P1": len(identities.get("P0", set()) & identities.get("P1", set())),
        "P0_P2": len(identities.get("P0", set()) & identities.get("P2", set())),
        "P1_P2": len(identities.get("P1", set()) & identities.get("P2", set())),
        "P0_P1_P2": len(
            identities.get("P0", set()) & identities.get("P1", set()) & identities.get("P2", set())
        ),
    }
    dedup = {
        name: {
            "exact_dropped": sum(row["reason"] == "exact_normalized" and row["decision"] == "drop" for row in value["dedup_audit"]),
            "near_dropped": sum(row["reason"] == "near_duplicate" and row["decision"] == "drop" for row in value["dedup_audit"]),
            "borderline_retained": sum(row["decision"] == "retain" for row in value["dedup_audit"]),
            "audit_path": f"{name}/dedup_audit.jsonl",
            "backfill_gap": int(value.get("backfill_gap", 0)),
        }
        for name, value in results.items()
    }
    return {
        "config_version": config.config_version,
        "config": asdict(config),
        "completed_sets": sorted(results),
        "sets": {
            name: {
                **value["audit"],
                "final": len(value["records"]),
                "target": None if name == "P0" else (
                    config.sampling.p1.target_n if name == "P1" else config.sampling.p2.target_n
                ),
                "deficit": 0 if name == "P0" else max(0, (
                    config.sampling.p1.target_n if name == "P1" else config.sampling.p2.target_n
                ) - len(value["records"])),
            }
            for name, value in results.items()
        },
        "p1_supplement": results.get("P1", {}).get("p1_supplement"),
        "p2_adaptive": results.get("P2", {}).get("p2_diagnostics"),
        "cross_set_overlap": overlap,
        "dedup": dedup,
    }


def run_build(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
    logger: logging.Logger | None = None,
    idf_snapshot: Mapping[str, Any] | None = None,
    on_branch_complete: Callable[[str, JsonObject], None] | None = None,
) -> BuildOutcome:
    """Run P0/P1/P2 concurrently with branch-local checkpoints and failure isolation."""
    run_root.mkdir(parents=True, exist_ok=True)
    active_logger = logger or logging.getLogger(__name__)
    idf_path = run_root / "dedup_idf.json"
    frozen_idf = (
        dict(idf_snapshot) if idf_snapshot is not None
        else json.loads(idf_path.read_text(encoding="utf-8")) if idf_path.is_file()
        else load_or_build_idf_snapshot(index, source_path)
    )
    validate_idf_snapshot(frozen_idf, index)
    if idf_path.is_file():
        if json.loads(idf_path.read_text(encoding="utf-8")) != frozen_idf:
            raise ValueError("运行目录冻结的 dedup IDF 与当前输入不一致")
    else:
        write_json(idf_path, frozen_idf)
    run_contract = {
        "config_version": config.config_version,
        "branch_algorithm": "independent_concurrent_branches_1",
        "p1_supplement_algorithm": "adaptive_cluster_batches_1",
        "p2_supplement_algorithm": P2_ALGORITHM,
        "config": asdict(config),
        "source_snapshot_id": index.metadata.get("source_snapshot_id"),
        "taxonomy_sha256": index.metadata.get("taxonomy_sha256"),
        "dedup_rules_sha256": RULES_SHA256,
        "dedup_idf_sha256": frozen_idf["content_sha256"],
    }
    contract_path = run_root / "p012_run_contract.json"
    if contract_path.is_file():
        if json.loads(contract_path.read_text(encoding="utf-8")) != run_contract:
            raise ValueError("P012 运行目录与并发分支契约不兼容")
    else:
        write_json(contract_path, run_contract)

    runners = {"P0": _run_p0_branch, "P1": _run_p1_branch, "P2": _run_p2_branch}
    results: dict[str, JsonObject] = {}
    errors: dict[str, str] = {}
    started = {name: time.monotonic_ns() for name in runners}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="screening-branch") as executor:
        futures = {
            executor.submit(
                runner, index, config, source_path=source_path, taxonomy=taxonomy,
                run_root=run_root, quality_runner=quality_runner, idf_snapshot=frozen_idf,
            ): name
            for name, runner in runners.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                branch_result = future.result()
                duration_ms = round((time.monotonic_ns() - started[name]) / 1_000_000)
                manifest_path = run_root / "manifest.json"
                current_manifest = (
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                    if manifest_path.is_file() else {}
                )
                current_status = (
                    current_manifest.get("branches", {}).get(name, {}).get("status")
                )
                terminal_status = "published" if current_status == "published" else "completed"
                _set_branch_status(
                    run_root, name, terminal_status, stage=terminal_status,
                    duration_ms=duration_ms, records=len(branch_result["records"]),
                    artifacts=branch_result.get("artifacts", {}),
                )
                if on_branch_complete is not None:
                    on_branch_complete(name, branch_result)
                results[name] = branch_result
                active_logger.info("branch complete set=%s records=%d", name, len(results[name]["records"]))
            except Exception as exc:  # keep other branches running
                errors[name] = f"{type(exc).__name__}: {exc}"
                _set_branch_status(
                    run_root, name, "failed", stage="failed",
                    duration_ms=round((time.monotonic_ns() - started[name]) / 1_000_000),
                    error_type=type(exc).__name__, error=str(exc),
                )
                active_logger.exception("branch failed set=%s", name)

    summary = _aggregate_concurrent_results(config, results)
    summary["failed_sets"] = dict(errors)
    write_json(run_root / "p012_build_summary.json", summary)
    write_json(
        run_root / "legacy_export_report.json",
        {
            "legacy_schema": "labeled_v5",
            "legacy_mapping_version": LEGACY_MAPPING_VERSION,
            "sets": {name: value["export_report"] for name, value in results.items()},
            "total_records": sum(len(value["records"]) for value in results.values()),
            "lossy_enum_mappings": 0,
            "failed_sets": dict(errors),
        },
    )
    output = {name: tuple(value["records"]) for name, value in results.items()}
    if not errors:
        evaluated = {name: results[name]["evaluated_candidates"] for name in ("P0", "P1", "P2")}
        evaluation_rounds = {name: results[name]["evaluation_rounds"] for name in ("P0", "P1", "P2")}
        stage1: dict[str, JsonObject] = {}
        for name in ("P0", "P1", "P2"):
            stage1.update(results[name]["stage1_results"])
        if not (run_root / "screening_run_report.json").is_file():
            write_run_report(
                run_root,
                run_id=run_root.name,
                taxonomy=taxonomy,
                evaluated_candidates=evaluated,
                evaluation_rounds=evaluation_rounds,
                stage1_results=stage1,
                final_records=output,
                p1_target_quotas=results["P1"]["p1_target_quotas"],
                build_summary=summary,
            )
    else:
        write_json(
            run_root / "screening_partial_report.json",
            {
                "report_schema_version": RUN_REPORT_SCHEMA_VERSION,
                "run_id": run_root.name,
                "completed_sets": sorted(results),
                "failed_sets": dict(errors),
                "sets": {
                    name: {"final_count": len(value["records"]), "status": "completed"}
                    for name, value in results.items()
                },
                "cross_set_overlap": summary["cross_set_overlap"],
            },
        )
    return BuildOutcome(records=output, summary=summary, errors=errors)
