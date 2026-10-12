"""P0/P1/P2 orchestration, admission, P1 supplement, and final construction."""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..candidate.index import CanonicalCandidateIndex, CanonicalIndexHandle
from ..candidate.contract import validate_canonical_candidate
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
from ..delivery.report import write_run_report
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


BUILD_CONFIG_VERSION = 3


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


def _usable_stage1(result: Mapping[str, Any]) -> bool:
    return (
        result["semantic_completeness"] == "self_contained"
        and result["intent_clarity"] == "clear"
        and result["validity"] == "valid"
    )


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


def _artifact_snapshot(run_root: Path, paths: Sequence[Path]) -> JsonObject:
    files: JsonObject = {}
    for path in paths:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(run_root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"阶段产物不在 run 目录内: {path}") from exc
        if not resolved.is_file():
            raise ValueError(f"阶段产物缺失: {relative}")
        files[relative] = {
            "sha256": sha256_file(resolved),
            "bytes": resolved.stat().st_size,
        }
    return {"files": files}


def _validate_artifact_snapshot(run_root: Path, snapshot: Mapping[str, Any]) -> None:
    files = snapshot.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("阶段产物检查点没有文件")
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, dict):
            raise ValueError("阶段产物检查点格式非法")
        path = (run_root / relative).resolve()
        try:
            path.relative_to(run_root.resolve())
        except ValueError as exc:
            raise ValueError("阶段产物检查点包含越界路径") from exc
        if (
            not path.is_file()
            or expected.get("bytes") != path.stat().st_size
            or expected.get("sha256") != sha256_file(path)
        ):
            raise ValueError(f"阶段产物缺失或哈希不一致: {relative}")


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
                request_n = min(p2_config.batch_size, budget - used)
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
                    stage1_started = time.monotonic()
                    batch_stage1 = quality_runner.run_stage1(new_candidates) if new_candidates else {}
                    stage1_duration_ms = round((time.monotonic() - stage1_started) * 1000)
                    stage1_passed = _passing(new_candidates, batch_stage1) if new_candidates else []
                    challenge_started = time.monotonic()
                    batch_challenge = (
                        quality_runner.run_challenge(stage1_passed) if stage1_passed else {}
                    )
                    challenge_duration_ms = round(
                        (time.monotonic() - challenge_started) * 1000
                    )
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
                batch_summaries.append({
                    "batch": batch_id,
                    "requested": sum(requested.values()),
                    "requested_by_source": requested,
                    "sampled": len(sampled_ids),
                    "sampled_by_source": dict(sampled_by_source),
                    "after_dedup": len(new_candidates),
                    "after_dedup_by_source": dict(after_dedup_by_source),
                    "stage1_pass": len(stage1_passed),
                    "stage1_pass_by_source": dict(stage1_by_source),
                    "judge_pass": challenge_passed,
                    "judge_pass_by_source": dict(challenge_by_source),
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
                })
                logger.info(
                    "P2 wave=%d batch=%d requested=%d evaluated=%d stage1_pass=%d judge_pass=%d final=%d",
                    wave,
                    batch_id,
                    sum(requested.values()),
                    len(new_candidates),
                    len(stage1_passed),
                    challenge_passed,
                    len(selected),
                )
                requested_total += sum(requested.values())
                # Overlapping source pools can make a requested slot unavailable
                # after an earlier source claims the same identity.  Count actual
                # draws against the wave budget so the next batch can refill it.
                used += len(sampled_ids)
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
        source_remaining = sampler.capacities(processed)
        exhausted = exhausted or not any(source_remaining.values())

    selected, selection_diag = select_final_p2(evaluated, challenge_results, p2_config)
    selection_rounds = {
        str(candidate["source_identity_sha256"]): evaluation_rounds[str(candidate["source_identity_sha256"])]
        for candidate in selected
    }
    if selection_diag["target_gap"] == 0 and not any(selection_diag["soft_min_deficit_by_primary"].values()):
        stop_reason = "target_and_soft_mins_met"
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
        "soft_min_per_type": p2_config.soft_min_per_type,
        "max_per_primary": p2_config.max_per_primary,
        "target_gap": selection_diag["target_gap"],
        "candidate_exhausted": exhausted,
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
        "P2 adaptive complete final=%d target_gap=%d stop=%s",
        len(selected), selection_diag["target_gap"], stop_reason,
    )
    return result


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
) -> BuildOutcome:
    """Run B-E as a contract-bound, stage-checkpointed state machine."""
    run_root.mkdir(parents=True, exist_ok=True)
    active_logger = logger or logging.getLogger(__name__)
    idf_path = run_root / "dedup_idf.json"
    idf_snapshot = (dict(idf_snapshot) if idf_snapshot is not None else
                    json.loads(idf_path.read_text(encoding="utf-8")) if idf_path.is_file() else
                    load_or_build_idf_snapshot(index, source_path))
    validate_idf_snapshot(idf_snapshot, index)
    if idf_path.is_file():
        if json.loads(idf_path.read_text(encoding="utf-8")) != idf_snapshot:
            raise ValueError("Frozen dedup IDF snapshot changed")
    else:
        write_json(idf_path, idf_snapshot)
    run_contract: JsonObject = {
        "config_version": config.config_version,
        "p1_supplement_algorithm": "adaptive_cluster_batches_1",
        "p2_supplement_algorithm": P2_ALGORITHM,
        "config": asdict(config),
        "source_snapshot_id": index.metadata.get("source_snapshot_id"),
        "taxonomy_sha256": index.metadata.get("taxonomy_sha256"),
        "canonical_index_version": index.metadata.get("canonical_index_version"),
        "feature_rule_version": index.metadata.get("feature_rule_version"),
        "dedup_rules_sha256": RULES_SHA256,
        "dedup_idf_sha256": idf_snapshot["content_sha256"],
    }
    contract_path = run_root / "p012_run_contract.json"
    if contract_path.is_file():
        existing_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        if existing_contract != run_contract:
            raise ValueError("P012 运行目录与当前输入、索引或配置不兼容")
    else:
        write_json(contract_path, run_contract)
    checkpoints = CheckpointStore(run_root, run_contract)

    def manifest_stage_status(stage: str) -> str | None:
        path = run_root / "manifest.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("stages", {}).get(stage, {}).get("status")

    def manifest_stage_value(stage: str, key: str, default: Any = None) -> Any:
        path = run_root / "manifest.json"
        if not path.is_file():
            return default
        value = json.loads(path.read_text(encoding="utf-8"))
        return value.get("stages", {}).get(stage, {}).get(key, default)

    def load_stage_checkpoint(name: str, stage: str) -> JsonObject | None:
        payload = checkpoints.load(name)
        if payload is None and manifest_stage_status(stage) == "completed":
            raise ValueError(f"manifest 标记阶段已完成，但检查点缺失: {stage}")
        return payload

    def mark(stage: str, status: str, **details: Any) -> None:
        if not (run_root / "manifest.json").is_file():
            return
        from .run_context import update_manifest

        update_manifest(
            run_root,
            stages={stage: {"status": status, "error": None, **details}},
        )

    def mark_branch(set_name: str, status: str, **details: Any) -> None:
        if not (run_root / "manifest.json").is_file():
            return
        from .run_context import update_manifest

        update_manifest(
            run_root,
            branches={set_name: {"status": status, "error": None, **details}},
        )

    completed = checkpoints.load("build_outcome")
    if completed is not None:
        for checkpoint_name, stage_name in (
            ("screened_export", "screened_export"),
            ("legacy_export", "legacy_export"),
            ("batch_report", "batch_report"),
        ):
            artifact_checkpoint = load_stage_checkpoint(checkpoint_name, stage_name)
            if artifact_checkpoint is None:
                raise ValueError(f"完整构建缺少阶段检查点: {checkpoint_name}")
            _validate_artifact_snapshot(run_root, artifact_checkpoint)
        active_logger.info("resume checkpoint=build_outcome records=%d", sum(
            len(completed["records"][name]) for name in ("P0", "P1", "P2")
        ))
        restored = {
            name: tuple(completed["records"][name]) for name in ("P0", "P1", "P2")
        }
        for name, records in restored.items():
            validate_screened_canonical_records(name, records)
            mark(f"sampling_{name.lower()}", "completed", restored=True)
            mark_branch(name, "completed", restored=True)
        for stage in (
            "quality_stage1_initial", "quality_stage1", "p1_supplement",
            "p2_supplement", "quality_p2_challenge",
            "final_membership", "quality_stage2", "screened_export", "legacy_export",
            "batch_report",
        ):
            mark(stage, "completed", restored=True)
        return BuildOutcome(records=restored, summary=completed["summary"])

    materialized: dict[str, JsonObject] = {}
    final_state = load_stage_checkpoint("final_membership", "final_membership")
    if final_state is None:
        branch_states: dict[str, JsonObject] = {}
        missing: list[str] = []
        for set_name in ("P0", "P1"):
            state = load_stage_checkpoint(
                f"initial_{set_name.lower()}", f"sampling_{set_name.lower()}"
            )
            if state is None:
                missing.append(set_name)
            else:
                for candidate in state["candidates"]:
                    validate_canonical_candidate(candidate)
                branch_states[set_name] = state
                mark(f"sampling_{set_name.lower()}", "completed", restored=True)
                mark_branch(set_name, "sampled", restored=True)
                active_logger.info(
                    "resume sampling set=%s candidates=%d",
                    set_name,
                    len(state["candidates"]),
                )

        if missing:
            active_logger.info("initial sampling start missing=%s", ",".join(missing))
            with ThreadPoolExecutor(max_workers=len(missing)) as executor:
                futures = {}
                for set_name in missing:
                    started = time.monotonic()
                    future = executor.submit(
                        _sample_branch,
                        set_name,
                        index,
                        source_path,
                        taxonomy,
                        config.sampling,
                    )
                    futures[future] = (set_name, started)
                for future in as_completed(futures):
                    set_name, started = futures[future]
                    locators = future.result()
                    raw = _materialize(index, source_path, taxonomy, locators, materialized)
                    dedup_events: list[JsonObject] = []
                    candidates = _deduplicate(
                        set_name,
                        raw,
                        config=config.sampling.dedup,
                        idf_snapshot=idf_snapshot,
                        audit=dedup_events,
                    )
                    sampled_ids = [locator["source_identity_sha256"] for locator in locators]
                    p0_gap = 0
                    if set_name == "P0":
                        target_by_cluster = Counter(item["cluster_id"] for item in locators)
                        while True:
                            kept_by_cluster = Counter(item["taxonomy"]["cluster_id"] for item in candidates)
                            deficits = {key: count - kept_by_cluster[key] for key, count in target_by_cluster.items()
                                        if count > kept_by_cluster[key]}
                            if not deficits:
                                break
                            replacements = _sample_branch(
                                "P0", index, source_path, taxonomy, config.sampling,
                                sampled_ids, deficits,
                            )
                            if not replacements:
                                p0_gap = sum(deficits.values())
                                break
                            sampled_ids.extend(item["source_identity_sha256"] for item in replacements)
                            replacement_raw = _materialize(index, source_path, taxonomy, replacements, materialized)
                            additions = _deduplicate(
                                "P0", replacement_raw, candidates,
                                config=config.sampling.dedup,
                                idf_snapshot=idf_snapshot, audit=dedup_events,
                            )
                            candidates.extend(additions)
                    state = {
                        "set": set_name,
                        "sampled_count": len(sampled_ids),
                        "sampled_identities": sampled_ids,
                        "candidates": candidates,
                        "dedup_audit": dedup_events,
                        "dedup_backfill_gap": p0_gap,
                    }
                    checkpoints.save(f"initial_{set_name.lower()}", state)
                    branch_states[set_name] = state
                    elapsed = time.monotonic() - started
                    mark(
                        f"sampling_{set_name.lower()}",
                        "completed",
                        sampled=len(locators),
                        after_dedup=len(candidates),
                        duration_ms=round(elapsed * 1000),
                    )
                    mark_branch(
                        set_name,
                        "sampled",
                        sampled=len(locators),
                        after_dedup=len(candidates),
                    )
                    active_logger.info(
                        "initial sampling complete set=%s sampled=%d after_dedup=%d seconds=%.2f",
                        set_name,
                        len(locators),
                        len(candidates),
                        elapsed,
                    )

        candidate_sets = {
            "P0": list(branch_states["P0"]["candidates"]),
            "P1": list(branch_states["P1"]["candidates"]),
            "P2": [],
        }
        dedup_events_by_set = {
            name: list(branch_states[name]["dedup_audit"]) for name in ("P0", "P1")
        }
        for set_name, candidates in candidate_sets.items():
            branch_root = run_root / set_name
            branch_root.mkdir(parents=True, exist_ok=True)
            _merge_candidate_tasks(
                branch_root / "candidate_tasks.jsonl",
                [_candidate_task(candidate, set_name, 0) for candidate in candidates],
            )

        initial_all = [candidate for values in candidate_sets.values() for candidate in values]
        evaluated_candidates = {
            name: list(candidate_sets[name]) for name in ("P0", "P1", "P2")
        }
        evaluation_rounds: dict[str, dict[str, int]] = {
            name: {candidate["source_identity_sha256"]: 0 for candidate in values}
            for name, values in candidate_sets.items()
        }
        stage1_checkpoint = load_stage_checkpoint(
            "stage1_initial", "quality_stage1_initial"
        )
        if stage1_checkpoint is None:
            mark("quality_stage1_initial", "running")
            active_logger.info("stage1 initial start candidates=%d", len(initial_all))
            stage1_started = time.monotonic()
            stage1 = quality_runner.run_stage1(initial_all)
            checkpoints.save("stage1_initial", {"results": stage1})
            stage1_elapsed = time.monotonic() - stage1_started
            mark(
                "quality_stage1_initial",
                "completed",
                results=len(stage1),
                duration_ms=round(stage1_elapsed * 1000),
            )
            active_logger.info(
                "stage1 initial complete results=%d seconds=%.2f",
                len(stage1),
                stage1_elapsed,
            )
        else:
            stage1 = dict(stage1_checkpoint["results"])
            mark("quality_stage1_initial", "completed", restored=True)
            active_logger.info("resume checkpoint=stage1_initial results=%d", len(stage1))

        accepted = {
            "P0": _passing(candidate_sets["P0"], stage1),
            "P1": _passing(candidate_sets["P1"], stage1),
            "P2": [],
        }
        selection_rounds: dict[str, dict[str, int]] = {
            name: {candidate["source_identity_sha256"]: 0 for candidate in values}
            for name, values in accepted.items()
        }
        audits: dict[str, JsonObject] = {
            name: _branch_audit(
                name,
                int(branch_states[name]["sampled_count"]),
                len(candidate_sets[name]),
                candidate_sets[name],
                stage1,
            )
            for name in ("P0", "P1")
        }

        quota_checkpoint = checkpoints.load("p1_quotas")
        if quota_checkpoint is None:
            active_logger.info("P1 quota freeze start")
            with CanonicalCandidateIndex(index) as readonly_index:
                capacities = readonly_index.available_counts("l2")
            p1_target_quotas = allocate_sqrt_quotas(
                taxonomy.l2_population,
                config.sampling.p1.target_n,
                capacities=capacities,
            )
            checkpoints.save("p1_quotas", {"quotas": p1_target_quotas})
        else:
            p1_target_quotas = {
                str(key): int(value) for key, value in quota_checkpoint["quotas"].items()
            }
            active_logger.info("resume checkpoint=p1_quotas l2=%d", len(p1_target_quotas))

        initial_p1_stage1_pass_count = len(accepted["P1"])
        capped_initial: list[JsonObject] = []
        capped_counts: Counter[str] = Counter()
        for candidate in accepted["P1"]:
            l2_key = candidate["taxonomy"]["l2_key"]
            if capped_counts[l2_key] < p1_target_quotas.get(l2_key, 0):
                capped_initial.append(candidate)
                capped_counts[l2_key] += 1
        accepted["P1"] = capped_initial
        initial_p1_admitted_count = len(capped_initial)
        selection_rounds["P1"] = {
            candidate["source_identity_sha256"]: 0 for candidate in capped_initial
        }
        initial_terminal = _l2_counts(candidate_sets["P1"])
        initial_pass = _l2_counts(accepted["P1"])
        supplement_terminal: Counter[str] = Counter()
        supplement_pass: Counter[str] = Counter()
        processed = set(branch_states["P1"]["sampled_identities"])
        cluster_feedback: dict[Any, list[int]] = {}
        _update_cluster_feedback(cluster_feedback, candidate_sets["P1"], stage1)
        with CanonicalCandidateIndex(index) as readonly_index:
            cluster_remaining = readonly_index.available_counts("cluster")
            for cluster, count in readonly_index.cluster_counts_for_identities(processed).items():
                cluster_remaining[cluster] -= count
                if cluster_remaining[cluster] < 0:
                    raise ValueError("P1 初采后 cluster 剩余量为负")
        supplement_rounds: list[JsonObject] = []
        mark("p1_supplement", "running", completed_rounds=0)
        supplement_started = time.monotonic()
        active_logger.info(
            "P1 supplement start initial_admitted=%d target=%d",
            len(accepted["P1"]),
            config.sampling.p1.target_n,
        )

        for round_id in range(1, config.p1_supplement.max_rounds + 1):
            round_started = time.monotonic()
            current = _l2_counts(accepted["P1"])
            deficits = {
                key: max(0, quota - current.get(key, 0))
                for key, quota in p1_target_quotas.items()
            }
            if not any(deficits.values()):
                break
            rates = _pass_rates(
                list(p1_target_quotas),
                initial_terminal,
                initial_pass,
                supplement_terminal,
                supplement_pass,
                config.p1_supplement,
            )
            requested = {
                key: math.ceil(deficit / rates[key] * config.p1_supplement.safety_factor)
                for key, deficit in deficits.items()
                if deficit > 0
            }
            round_cluster_counts = {
                key: sum(
                    cluster_remaining.get(cluster, 0) > 0
                    for cluster in taxonomy.l2_clusters.get(key, ())
                )
                for key in requested
            }
            round_plan_name = f"p1_round_{round_id}_request"
            round_plan = checkpoints.load(round_plan_name)
            completed_rounds = int(
                manifest_stage_value("p1_supplement", "completed_rounds", 0) or 0
            )
            if round_plan is None and completed_rounds >= round_id:
                raise ValueError(
                    f"manifest 标记 P1 第 {round_id} 轮已完成，但计划检查点缺失"
                )
            if round_plan is None:
                round_plan = {
                    "round": round_id,
                    "requested_by_l2": requested,
                    "estimated_pass_rate_by_l2": rates,
                    "available_cluster_count_by_l2": round_cluster_counts,
                }
                checkpoints.save(round_plan_name, round_plan)
            elif (
                round_plan["requested_by_l2"] != requested
                or round_plan["available_cluster_count_by_l2"] != round_cluster_counts
            ):
                raise ValueError(f"P1 第 {round_id} 轮计划与续跑状态不一致")
            active_logger.info(
                "P1 supplement round=%d sampling requested=%d",
                round_id, sum(requested.values()),
            )
            used: Counter[str] = Counter()
            round_draws: Counter[Any] = Counter()
            round_cold: Counter[Any] = Counter()
            batch_summaries: list[JsonObject] = []
            batch_id = 0
            while True:
                current = _l2_counts(accepted["P1"])
                deficits = {
                    key: max(0, quota - current.get(key, 0))
                    for key, quota in p1_target_quotas.items()
                }
                batch_request = {
                    key: min(deficit, requested[key] - used[key])
                    for key, deficit in deficits.items()
                    if deficit > 0
                    and used[key] < requested.get(key, 0)
                    and any(cluster_remaining.get(cluster, 0) > 0
                            for cluster in taxonomy.l2_clusters.get(key, ()))
                }
                if not batch_request:
                    break
                batch_id += 1
                plan_name = f"p1_round_{round_id}_batch_{batch_id}_plan"
                plan = checkpoints.load(plan_name)
                if plan is None and completed_rounds >= round_id:
                    raise ValueError(f"P1 第 {round_id} 轮第 {batch_id} 批计划检查点缺失")
                if plan is None:
                    with CanonicalCandidateIndex(index) as readonly_index:
                        sampler = CandidateSampler(
                            readonly_index, source_path, taxonomy, config.sampling
                        )
                        locators = sampler.sample_p1_supplement_batch(
                            batch_request,
                            processed,
                            remaining_by_cluster=cluster_remaining,
                            evaluated_passed_by_cluster=cluster_feedback,
                            prior_by_l2=rates,
                            prior_strength=config.p1_supplement.pass_rate_prior_strength,
                            round_draws_by_cluster=round_draws,
                            round_cold_by_cluster=round_cold,
                            round_request_by_l2=requested,
                            round_cluster_count_by_l2=round_cluster_counts,
                        )
                    if not locators:
                        raise ValueError("P1 仍有可抽样候选，但小批采样结果为空")
                    new_materialized = _materialize(
                        index, source_path, taxonomy, locators, materialized
                    )
                    batch_dedup: list[JsonObject] = []
                    new_candidates = _deduplicate(
                        "P1", new_materialized, evaluated_candidates["P1"],
                        config=config.sampling.dedup,
                        idf_snapshot=idf_snapshot, audit=batch_dedup,
                    )
                    sampled_l2 = Counter(item["l2_key"] for item in locators)
                    sampled_cluster = Counter(
                        (item["l2_key"], item["cluster_id"]) for item in locators
                    )
                    cold_cluster = Counter(
                        item["cluster_id"] for item in locators
                        if item["features"]["probability_band"] == "boundary"
                        and item["features"]["frequency_band"] == "cold"
                    )
                    plan = {
                        "round": round_id,
                        "batch": batch_id,
                        "requested_by_l2": batch_request,
                        "sampled_identities": [item["source_identity_sha256"] for item in locators],
                        "sampled_by_l2": dict(sampled_l2),
                        "sampled_by_cluster": [
                            {"l2_key": key, "cluster_id": cluster, "count": count}
                            for (key, cluster), count in sorted(
                                sampled_cluster.items(), key=lambda item: (item[0][0], str(item[0][1]))
                            )
                        ],
                        "cold_by_cluster": [
                            {"cluster_id": cluster, "count": count}
                            for cluster, count in sorted(cold_cluster.items(), key=lambda item: str(item[0]))
                        ],
                        "candidates": new_candidates,
                        "dedup_audit": batch_dedup,
                    }
                    checkpoints.save(plan_name, plan)
                elif plan["requested_by_l2"] != batch_request:
                    raise ValueError(f"P1 第 {round_id} 轮第 {batch_id} 批计划与续跑状态不一致")
                processed.update(plan["sampled_identities"])
                dedup_events_by_set["P1"].extend(plan["dedup_audit"])
                used.update(plan["sampled_by_l2"])
                for row in plan["sampled_by_cluster"]:
                    cluster = row["cluster_id"]
                    count = int(row["count"])
                    round_draws[cluster] += count
                    cluster_remaining[cluster] -= count
                    if cluster_remaining[cluster] < 0:
                        raise ValueError("P1 补采后 cluster 剩余量为负")
                for row in plan["cold_by_cluster"]:
                    round_cold[row["cluster_id"]] += int(row["count"])
                new_candidates = list(plan["candidates"])
                if new_candidates:
                    _merge_candidate_tasks(
                        run_root / "P1" / "candidate_tasks.jsonl",
                        [_candidate_task(candidate, "P1", round_id) for candidate in new_candidates],
                    )
                    evaluated_candidates["P1"].extend(new_candidates)
                    evaluation_rounds["P1"].update(
                        {candidate["source_identity_sha256"]: round_id for candidate in new_candidates}
                    )
                result_name = f"p1_round_{round_id}_batch_{batch_id}_result"
                batch_result = checkpoints.load(result_name)
                if batch_result is None and completed_rounds >= round_id:
                    raise ValueError(f"P1 第 {round_id} 轮第 {batch_id} 批结果检查点缺失")
                if batch_result is None:
                    batch_stage1 = quality_runner.run_stage1(new_candidates) if new_candidates else {}
                    batch_pass = _passing(new_candidates, batch_stage1) if new_candidates else []
                    pass_counts = _l2_counts(batch_pass)
                    if any(pass_counts[key] > deficits[key] for key in pass_counts):
                        raise ValueError("P1 小批通过量超过当前 L2 缺口")
                    batch_result = {
                        "stage1_results": batch_stage1,
                        "admitted_identities": [item["source_identity_sha256"] for item in batch_pass],
                    }
                    checkpoints.save(result_name, batch_result)
                batch_stage1 = dict(batch_result["stage1_results"])
                stage1.update(batch_stage1)
                batch_pass = _passing(new_candidates, batch_stage1) if new_candidates else []
                admitted_ids = set(batch_result["admitted_identities"])
                if admitted_ids != {item["source_identity_sha256"] for item in batch_pass}:
                    raise ValueError("P1 小批通过与入集检查点不一致")
                accepted["P1"].extend(batch_pass)
                selection_rounds["P1"].update(
                    {item["source_identity_sha256"]: round_id for item in batch_pass}
                )
                supplement_terminal.update(_l2_counts(new_candidates))
                supplement_pass.update(_l2_counts(batch_pass))
                _update_cluster_feedback(cluster_feedback, new_candidates, batch_stage1)
                diagnostic_l2: dict[str, JsonObject] = {
                    key: {"requested": count, "sampled": int(plan["sampled_by_l2"].get(key, 0)),
                          "evaluated": 0, "usable": 0, "exact": 0, "stage1_pass": 0,
                          "admitted": 0}
                    for key, count in batch_request.items()
                }
                diagnostic_cluster: dict[tuple[str, Any], JsonObject] = {
                    (row["l2_key"], row["cluster_id"]): {
                        "l2_key": row["l2_key"], "cluster_id": row["cluster_id"],
                        "sampled": int(row["count"]), "evaluated": 0, "usable": 0,
                        "exact": 0, "stage1_pass": 0, "admitted": 0,
                    }
                    for row in plan["sampled_by_cluster"]
                }
                for candidate in new_candidates:
                    identity = candidate["source_identity_sha256"]
                    key = candidate["taxonomy"]["l2_key"]
                    cluster = candidate["taxonomy"]["cluster_id"]
                    result = batch_stage1[identity]
                    for row in (diagnostic_l2[key], diagnostic_cluster[(key, cluster)]):
                        row["evaluated"] += 1
                        row["usable"] += int(_usable_stage1(result))
                        row["exact"] += int(_usable_stage1(result) and result["category_fit"] == "exact")
                        row["stage1_pass"] += int(stage1_passes(result))
                        row["admitted"] += int(identity in admitted_ids)
                current_after = _l2_counts(accepted["P1"])
                for key, row in diagnostic_l2.items():
                    row["remaining_deficit"] = max(0, p1_target_quotas[key] - current_after[key])
                    row["candidate_remaining"] = sum(
                        cluster_remaining.get(cluster, 0)
                        for cluster in taxonomy.l2_clusters.get(key, ())
                    )
                    row["stop_reason"] = (
                        "quota_filled" if row["remaining_deficit"] == 0
                        else "candidate_exhausted" if row["candidate_remaining"] == 0
                        else "round_requests_used" if used[key] >= requested[key]
                        else "continue"
                    )
                for (_, cluster), row in diagnostic_cluster.items():
                    row["candidate_remaining"] = cluster_remaining[cluster]
                batch_summaries.append({
                    "batch": batch_id,
                    "sampled": len(plan["sampled_identities"]),
                    "after_dedup": len(new_candidates),
                    "stage1_pass": len(batch_pass),
                    "admitted": len(batch_pass),
                    "l2": diagnostic_l2,
                    "clusters": [diagnostic_cluster[key] for key in sorted(
                        diagnostic_cluster, key=lambda item: (item[0], str(item[1]))
                    )],
                })
                active_logger.info(
                    "P1 supplement round=%d batch=%d sampled=%d evaluated=%d admitted=%d",
                    round_id, batch_id, len(plan["sampled_identities"]),
                    len(new_candidates), len(batch_pass),
                )
            round_final_counts = _l2_counts(accepted["P1"])
            remaining_deficits = {
                key: max(0, quota - round_final_counts[key])
                for key, quota in p1_target_quotas.items()
            }
            if not any(remaining_deficits.values()):
                round_stop = "quota_filled"
            elif all(
                not any(cluster_remaining.get(cluster, 0) > 0
                        for cluster in taxonomy.l2_clusters.get(key, ()))
                for key, deficit in remaining_deficits.items() if deficit > 0
            ):
                round_stop = "candidate_exhausted"
            else:
                round_stop = "round_requests_used"
            round_summary = {
                "round": round_id,
                "requested_by_l2": requested,
                "estimated_pass_rate_by_l2": rates,
                "sampled": sum(batch["sampled"] for batch in batch_summaries),
                "after_dedup": sum(batch["after_dedup"] for batch in batch_summaries),
                "stage1_pass": sum(batch["stage1_pass"] for batch in batch_summaries),
                "admitted": sum(batch["admitted"] for batch in batch_summaries),
                "batches": batch_summaries,
                "stop": round_stop,
            }
            summary_name = f"p1_round_{round_id}_summary"
            saved_summary = checkpoints.load(summary_name)
            if saved_summary is None:
                if completed_rounds >= round_id:
                    raise ValueError(f"P1 第 {round_id} 轮汇总检查点缺失")
                checkpoints.save(summary_name, round_summary)
            elif saved_summary != round_summary:
                raise ValueError(f"P1 第 {round_id} 轮汇总与续跑状态不一致")
            supplement_rounds.append(round_summary)
            active_logger.info(
                "P1 supplement round=%d complete sampled=%d stage1_pass=%d admitted=%d seconds=%.2f",
                round_id,
                round_summary["sampled"],
                round_summary["stage1_pass"],
                round_summary["admitted"],
                time.monotonic() - round_started,
            )
            mark(
                "p1_supplement",
                "running",
                completed_rounds=round_id,
                admitted=len(accepted["P1"]),
            )
            if round_stop == "candidate_exhausted":
                break

        mark("p2_supplement", "running")
        mark("quality_p2_challenge", "running")
        p2_started = time.monotonic()
        p2_state = _run_adaptive_p2(
            index,
            config,
            source_path=source_path,
            taxonomy=taxonomy,
            run_root=run_root,
            quality_runner=quality_runner,
            checkpoints=checkpoints,
            materialized=materialized,
            logger=active_logger,
            idf_snapshot=idf_snapshot,
        )
        dedup_events_by_set["P2"] = list(p2_state["dedup_audit"])
        accepted["P2"] = list(p2_state["selected"])
        evaluated_candidates["P2"] = list(p2_state["evaluated_candidates"])
        evaluation_rounds["P2"] = dict(p2_state["evaluation_rounds"])
        selection_rounds["P2"] = dict(p2_state["selection_rounds"])
        stage1.update(p2_state["stage1_results"])
        audits["P2"] = _branch_audit(
            "P2",
            sum(batch["sampled"] for value in p2_state["diagnostics"]["rounds"] for batch in value["batches"]),
            len(evaluated_candidates["P2"]),
            evaluated_candidates["P2"],
            stage1,
        )
        p2_elapsed = time.monotonic() - p2_started
        mark(
            "p2_supplement", "completed",
            rounds=max(0, len(p2_state["diagnostics"]["rounds"]) - 1),
            duration_ms=round(p2_elapsed * 1000),
        )
        mark(
            "quality_p2_challenge", "completed",
            results=len(p2_state["challenge_results"]),
        )
        mark("sampling_p2", "completed", sampled=p2_state["diagnostics"]["evaluated"])
        mark_branch("P2", "sampled", after_dedup=p2_state["diagnostics"]["evaluated"])

        final_state = {
            "sets": {name: accepted[name] for name in ("P0", "P1", "P2")},
            "stage1_results": stage1,
            "selection_rounds": selection_rounds,
            "evaluated_candidates": evaluated_candidates,
            "evaluation_rounds": evaluation_rounds,
            "audits": audits,
            "dedup_audits": dedup_events_by_set,
            "p0_dedup_backfill_gap": branch_states["P0"].get("dedup_backfill_gap", 0),
            "p1_target_quotas": p1_target_quotas,
            "supplement_rounds": supplement_rounds,
            "p1_candidate_remaining_by_l2": {
                key: sum(cluster_remaining.get(cluster, 0)
                         for cluster in taxonomy.l2_clusters.get(key, ()))
                for key in p1_target_quotas
            },
            "initial_p1_stage1_pass_count": initial_p1_stage1_pass_count,
            "initial_p1_admitted_count": initial_p1_admitted_count,
            "challenge_results": p2_state["challenge_results"],
            "p2_metadata": p2_state["metadata"],
            "p2_diagnostics": p2_state["diagnostics"],
        }
        checkpoints.save("final_membership", final_state)
        mark("quality_stage1", "completed", results=len(stage1))
        supplement_elapsed = time.monotonic() - supplement_started
        mark(
            "p1_supplement",
            "completed",
            rounds=len(supplement_rounds),
            duration_ms=round(supplement_elapsed * 1000),
        )
        mark("final_membership", "completed", records=sum(len(v) for v in accepted.values()))
        active_logger.info(
            "P1 supplement complete rounds=%d admitted=%d seconds=%.2f",
            len(supplement_rounds),
            len(accepted["P1"]),
            supplement_elapsed,
        )
        active_logger.info(
            "final membership checkpoint records=%d",
            sum(len(values) for values in accepted.values()),
        )
    else:
        active_logger.info("resume checkpoint=final_membership")
        for values in final_state["sets"].values():
            for candidate in values:
                validate_canonical_candidate(candidate)
        mark("quality_stage1", "completed", restored=True)
        mark("p1_supplement", "completed", restored=True)
        mark("p2_supplement", "completed", restored=True)
        mark("quality_p2_challenge", "completed", restored=True)
        mark("final_membership", "completed", restored=True)
        for name in ("P0", "P1", "P2"):
            mark(f"sampling_{name.lower()}", "completed", restored=True)
            mark_branch(name, "sampled", restored=True)

    accepted = {name: list(final_state["sets"][name]) for name in ("P0", "P1", "P2")}
    stage1 = dict(final_state["stage1_results"])
    selection_rounds = {
        name: {str(key): int(value) for key, value in final_state["selection_rounds"][name].items()}
        for name in ("P0", "P1", "P2")
    }
    evaluated_candidates = {
        name: list(final_state["evaluated_candidates"][name]) for name in ("P0", "P1", "P2")
    }
    evaluation_rounds = {
        name: {str(key): int(value) for key, value in final_state["evaluation_rounds"][name].items()}
        for name in ("P0", "P1", "P2")
    }
    audits = {name: dict(final_state["audits"][name]) for name in ("P0", "P1", "P2")}
    for name in ("P0", "P1", "P2"):
        branch_root = run_root / name
        branch_root.mkdir(parents=True, exist_ok=True)
        write_jsonl(branch_root / "dedup_audit.jsonl", final_state["dedup_audits"][name])
    p1_target_quotas = {
        str(key): int(value) for key, value in final_state["p1_target_quotas"].items()
    }
    supplement_rounds = list(final_state["supplement_rounds"])
    p1_candidate_remaining_by_l2 = {
        str(key): int(value)
        for key, value in final_state["p1_candidate_remaining_by_l2"].items()
    }
    initial_p1_stage1_pass_count = int(final_state["initial_p1_stage1_pass_count"])
    initial_p1_admitted_count = int(final_state["initial_p1_admitted_count"])
    challenge_results = {
        str(key): dict(value) for key, value in final_state["challenge_results"].items()
    }
    p2_metadata = {
        str(key): dict(value) for key, value in final_state["p2_metadata"].items()
    }
    p2_diagnostics = dict(final_state["p2_diagnostics"])
    for name in ("P0", "P1", "P2"):
        branch_root = run_root / name
        branch_root.mkdir(parents=True, exist_ok=True)
        _merge_candidate_tasks(
            branch_root / "candidate_tasks.jsonl",
            [
                {
                    **_candidate_task(
                        candidate,
                        name,
                        evaluation_rounds[name][candidate["source_identity_sha256"]],
                    ),
                    **(
                        p2_metadata[candidate["source_identity_sha256"]]
                        if name == "P2" else {}
                    ),
                }
                for candidate in evaluated_candidates[name]
            ],
        )

    final_all = [candidate for values in accepted.values() for candidate in values]
    final_stage1 = {
        candidate["source_identity_sha256"]: stage1[candidate["source_identity_sha256"]]
        for candidate in final_all
    }
    stage2_checkpoint = load_stage_checkpoint("stage2", "quality_stage2")
    if stage2_checkpoint is None:
        mark("quality_stage2", "running")
        active_logger.info("stage2 start final_candidates=%d", len(final_all))
        stage2_started = time.monotonic()
        stage2 = quality_runner.run_stage2(final_all, final_stage1)
        checkpoints.save("stage2", {"results": stage2})
        stage2_elapsed = time.monotonic() - stage2_started
        mark(
            "quality_stage2",
            "completed",
            results=len(stage2),
            duration_ms=round(stage2_elapsed * 1000),
        )
        active_logger.info(
            "stage2 complete results=%d seconds=%.2f",
            len(stage2),
            stage2_elapsed,
        )
    else:
        stage2 = dict(stage2_checkpoint["results"])
        mark("quality_stage2", "completed", restored=True)
        active_logger.info("resume checkpoint=stage2 results=%d", len(stage2))

    risk_snapshot = json.loads((run_root / "risk_keywords.json").read_text(encoding="utf-8"))
    output: dict[str, tuple[JsonObject, ...]] = {}
    for set_name in ("P0", "P1", "P2"):
        records = tuple(
            build_screened_canonical_record(
                set_name,
                candidate,
                stage1[candidate["source_identity_sha256"]],
                stage2[candidate["source_identity_sha256"]],
                delivery_labels(candidate, stage2[candidate["source_identity_sha256"]], risk_snapshot),
                challenge_result=(
                    challenge_results[candidate["source_identity_sha256"]]
                    if set_name == "P2" else None
                ),
                challenge_task_id=(
                    _quality_task_id(quality_runner, "challenge", candidate)
                    if set_name == "P2" else None
                ),
                origin=(
                    "initial_wave"
                    if selection_rounds[set_name][candidate["source_identity_sha256"]] == 0
                    else "supplement_wave"
                ),
                round_id=selection_rounds[set_name][candidate["source_identity_sha256"]],
                candidate_task_id=(
                    f"{set_name}:"
                    f"{selection_rounds[set_name][candidate['source_identity_sha256']]}:"
                    f"{candidate['source_identity_sha256']}"
                ),
                stage1_task_id=_quality_task_id(quality_runner, "stage1", candidate),
                stage2_task_id=_quality_task_id(
                    quality_runner,
                    "stage2",
                    candidate,
                    stage1[candidate["source_identity_sha256"]],
                ),
            )
            for candidate in accepted[set_name]
        )
        output[set_name] = records
        branch_root = run_root / set_name
        branch_root.mkdir(parents=True, exist_ok=True)
        tasks = {
            str(task["task_id"]): task
            for task in load_jsonl(branch_root / "candidate_tasks.jsonl")
        }
        validate_screened_canonical_records(
            set_name,
            records,
            expected_candidate_tasks=tasks,
            expected_final_count=len(accepted[set_name]),
        )
    screened_checkpoint = load_stage_checkpoint("screened_export", "screened_export")
    legacy_checkpoint = load_stage_checkpoint("legacy_export", "legacy_export")
    if (screened_checkpoint is None) != (legacy_checkpoint is None):
        raise ValueError("screened 与 legacy 导出检查点状态不一致")
    if screened_checkpoint is not None and legacy_checkpoint is not None:
        _validate_artifact_snapshot(run_root, screened_checkpoint)
        _validate_artifact_snapshot(run_root, legacy_checkpoint)
        export_reports = dict(legacy_checkpoint["reports"])
        mark("screened_export", "completed", restored=True)
        mark("legacy_export", "completed", restored=True)
        active_logger.info(
            "resume checkpoints=screened_export,legacy_export records=%d",
            sum(len(records) for records in output.values()),
        )
    else:
        export_started = time.monotonic()
        active_logger.info("screened and legacy export start")
        export_reports: dict[str, JsonObject] = {}
        for set_name in ("P0", "P1", "P2"):
            export_reports[set_name] = export_legacy_branch(
                set_name, output[set_name], run_root / set_name
            )
        root_export_report = run_root / "legacy_export_report.json"
        write_json(
            root_export_report,
            {
                "legacy_schema": "labeled_v5",
                "legacy_mapping_version": LEGACY_MAPPING_VERSION,
                "sets": export_reports,
                "total_records": sum(len(records) for records in output.values()),
                "lossy_enum_mappings": 0,
            },
        )
        screened_paths = [
            run_root / name / "screened.canonical.jsonl"
            for name in ("P0", "P1", "P2")
        ]
        legacy_paths = [root_export_report]
        for name in ("P0", "P1", "P2"):
            branch_root = run_root / name
            legacy_paths.extend(
                [
                    branch_root / export_reports[name]["files"]["legacy"],
                    branch_root / export_reports[name]["files"]["provenance"],
                    branch_root / "legacy_export_report.json",
                ]
            )
        checkpoints.save("screened_export", _artifact_snapshot(run_root, screened_paths))
        legacy_snapshot = _artifact_snapshot(run_root, legacy_paths)
        legacy_snapshot["reports"] = export_reports
        checkpoints.save("legacy_export", legacy_snapshot)
        export_elapsed = time.monotonic() - export_started
        export_count = sum(len(v) for v in output.values())
        mark(
            "screened_export",
            "completed",
            records=export_count,
            duration_ms=round(export_elapsed * 1000),
        )
        mark(
            "legacy_export",
            "completed",
            records=export_count,
            duration_ms=round(export_elapsed * 1000),
        )
        active_logger.info(
            "screened and legacy export complete records=%d seconds=%.2f",
            export_count,
            export_elapsed,
        )

    final_p1_counts = _l2_counts(accepted["P1"])
    p1_deficits = {
        key: max(0, quota - final_p1_counts.get(key, 0))
        for key, quota in p1_target_quotas.items()
    }
    unallocated_target = max(0, config.sampling.p1.target_n - sum(p1_target_quotas.values()))
    if sum(p1_target_quotas.values()) + unallocated_target != config.sampling.p1.target_n:
        raise ValueError("P1 冻结 L2 配额与目标数量不守恒")
    if any(final_p1_counts.get(key, 0) > quota for key, quota in p1_target_quotas.items()):
        raise ValueError("P1 最终 L2 数量超过冻结配额")
    if not any(p1_deficits.values()) and not unallocated_target:
        supplement_stop_reason = "quota_filled"
    elif supplement_rounds and supplement_rounds[-1].get("stop") == "candidate_exhausted":
        supplement_stop_reason = "candidate_exhausted"
    elif len(supplement_rounds) >= config.p1_supplement.max_rounds:
        supplement_stop_reason = "max_rounds"
    else:
        supplement_stop_reason = "candidate_exhausted"
    supplement_summary = {
        "enabled": True,
        "target": config.sampling.p1.target_n,
        "target_quotas_by_l2": p1_target_quotas,
        "rounds": supplement_rounds,
        "stop_reason": supplement_stop_reason,
        "final_count": len(accepted["P1"]),
        "unallocated_target": unallocated_target,
        "total_deficit": config.sampling.p1.target_n - len(accepted["P1"]),
        "deficits_by_l2": p1_deficits,
        "candidate_remaining_by_l2": p1_candidate_remaining_by_l2,
        "stop_reason_by_l2": {
            key: (
                "quota_filled" if p1_deficits[key] == 0
                else "candidate_exhausted" if p1_candidate_remaining_by_l2[key] == 0
                else "max_rounds"
            )
            for key in p1_target_quotas
        },
    }
    write_json(run_root / "P1" / "supplement_summary.json", supplement_summary)
    identities = {
        name: {record["candidate"]["source_identity_sha256"] for record in records}
        for name, records in output.items()
    }
    overlap = {
        "P0_P1": len(identities["P0"] & identities["P1"]),
        "P0_P2": len(identities["P0"] & identities["P2"]),
        "P1_P2": len(identities["P1"] & identities["P2"]),
        "P0_P1_P2": len(identities["P0"] & identities["P1"] & identities["P2"]),
    }
    dedup_summary = {
        name: {
            "exact_dropped": sum(row["reason"] == "exact_normalized" and row["decision"] == "drop"
                                 for row in final_state["dedup_audits"][name]),
            "near_dropped": sum(row["reason"] == "near_duplicate" and row["decision"] == "drop"
                                for row in final_state["dedup_audits"][name]),
            "borderline_retained": sum(row["decision"] == "retain"
                                       for row in final_state["dedup_audits"][name]),
            "audit_path": f"{name}/dedup_audit.jsonl",
            "backfill_gap": int(final_state["p0_dedup_backfill_gap"]) if name == "P0" else 0,
        }
        for name in ("P0", "P1", "P2")
    }
    summary: JsonObject = {
        "config_version": config.config_version,
        "config": asdict(config),
        "sets": {
            name: {
                **audits[name],
                "final": len(output[name]),
                "target": None if name == "P0" else (
                    config.sampling.p1.target_n if name == "P1" else config.sampling.p2.target_n
                ),
                "deficit": 0 if name == "P0" else (
                    config.sampling.p1.target_n - len(output["P1"])
                    if name == "P1" else max(0, config.sampling.p2.target_n - len(output[name]))
                ),
            }
            for name in ("P0", "P1", "P2")
        },
        "p1_supplement": supplement_summary,
        "p2_adaptive": p2_diagnostics,
        "cross_set_overlap": overlap,
        "dedup": dedup_summary,
    }
    supplement_sampled = sum(int(item.get("sampled", 0)) for item in supplement_rounds)
    supplement_after_dedup = sum(int(item.get("after_dedup", 0)) for item in supplement_rounds)
    supplement_stage1_pass = sum(int(item.get("stage1_pass", 0)) for item in supplement_rounds)
    supplement_admitted = sum(int(item.get("admitted", 0)) for item in supplement_rounds)
    audits["P1"].update(
        {
            "initial_stage1_pass": initial_p1_stage1_pass_count,
            "initial_admitted": initial_p1_admitted_count,
            "initial_pass_not_admitted": initial_p1_stage1_pass_count - initial_p1_admitted_count,
            "supplement_sampled": supplement_sampled,
            "supplement_after_dedup": supplement_after_dedup,
            "supplement_stage1_pass": supplement_stage1_pass,
            "supplement_admitted": supplement_admitted,
            "supplement_pass_not_admitted": supplement_stage1_pass - supplement_admitted,
            "final": len(output["P1"]),
            "target": config.sampling.p1.target_n,
            "deficit": config.sampling.p1.target_n - len(output["P1"]),
            "target_quota_sum": sum(p1_target_quotas.values()),
            "unallocated_target": unallocated_target,
        }
    )
    audits["P0"].update(
        {
            "supplement_sampled": 0,
            "supplement_after_dedup": 0,
            "supplement_stage1_pass": 0,
            "supplement_admitted": 0,
            "final": len(output["P0"]),
        }
    )
    audits["P2"].update(
        {
            "supplement_sampled": sum(
                batch["sampled"]
                for value in p2_diagnostics["rounds"][1:]
                for batch in value["batches"]
            ),
            "supplement_after_dedup": sum(
                batch["after_dedup"]
                for value in p2_diagnostics["rounds"][1:]
                for batch in value["batches"]
            ),
            "supplement_stage1_pass": sum(
                batch["stage1_pass"]
                for value in p2_diagnostics["rounds"][1:]
                for batch in value["batches"]
            ),
            "supplement_admitted": sum(
                batch["judge_pass"]
                for value in p2_diagnostics["rounds"][1:]
                for batch in value["batches"]
            ),
            "judge_pass": p2_diagnostics["judge_pass"],
            "judge_pass_not_admitted": p2_diagnostics["selection"]["judge_pass_not_selected"],
            "final": len(output["P2"]),
        }
    )
    for name in ("P0", "P1", "P2"):
        write_json(run_root / name / "branch_audit.json", audits[name])
        summary["sets"][name].update(audits[name])
    write_json(run_root / "p012_build_summary.json", summary)
    report_checkpoint = load_stage_checkpoint("batch_report", "batch_report")
    if report_checkpoint is not None:
        _validate_artifact_snapshot(run_root, report_checkpoint)
        mark("batch_report", "completed", restored=True)
        active_logger.info("resume checkpoint=batch_report")
    else:
        report_started = time.monotonic()
        active_logger.info("batch report start")
        write_run_report(
            run_root,
            run_id=run_root.name,
            taxonomy=taxonomy,
            evaluated_candidates=evaluated_candidates,
            evaluation_rounds=evaluation_rounds,
            stage1_results=stage1,
            final_records=output,
            p1_target_quotas=p1_target_quotas,
            build_summary=summary,
        )
        checkpoints.save(
            "batch_report",
            _artifact_snapshot(
                run_root,
                [run_root / "screening_run_report.json", run_root / "screening_run_report.md"],
            ),
        )
        report_elapsed = time.monotonic() - report_started
        mark(
            "batch_report",
            "completed",
            duration_ms=round(report_elapsed * 1000),
        )
        active_logger.info(
            "batch report complete seconds=%.2f", report_elapsed
        )
    checkpoints.save(
        "build_outcome",
        {"records": {name: list(records) for name, records in output.items()}, "summary": summary},
    )
    active_logger.info("build complete records=%d", sum(len(value) for value in output.values()))
    return BuildOutcome(records=output, summary=summary)
