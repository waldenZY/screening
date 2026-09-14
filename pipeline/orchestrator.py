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
from ..delivery.legacy import export_legacy_branch
from ..delivery.report import write_run_report
from ..delivery.screened import (
    build_screened_canonical_record,
    validate_screened_canonical_records,
)
from ..quality.two_stage import TwoStageQualityRunner, stage1_passes
from .checkpoints import CheckpointStore


BUILD_CONFIG_VERSION = 1


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
class BuildConfig:
    config_version: int = BUILD_CONFIG_VERSION
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    p1_supplement: P1SupplementConfig = field(default_factory=P1SupplementConfig)

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
    _only(value, {"config_version", "sampling", "p1_supplement"}, "配置")
    sampling = value.get("sampling", {})
    supplement = value.get("p1_supplement", {})
    if not isinstance(sampling, dict) or not isinstance(supplement, dict):
        raise ValueError("sampling 和 p1_supplement 必须是对象")
    _only(sampling, {"p0", "p1", "p2"}, "sampling")
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
    return BuildConfig(
        config_version=value.get("config_version", BUILD_CONFIG_VERSION),
        sampling=SamplingConfig(**sections),
        p1_supplement=supplement_config,
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
) -> list[JsonObject]:
    with CanonicalCandidateIndex(handle) as index:
        sampler = CandidateSampler(index, source_path, taxonomy, config)
        if set_name == "P0":
            return sampler.sample_p0()
        if set_name == "P1":
            return sampler.sample_p1()
        return sampler.sample_p2()


def _deduplicate(
    set_name: str,
    candidates: Sequence[JsonObject],
    existing: Sequence[JsonObject] = (),
    *,
    threshold: float | None = None,
) -> list[JsonObject]:
    return deduplicate_branch_candidates(
        set_name,
        candidates,
        existing,
        threshold=threshold,
        text_getter=lambda item: (
            item["conversation"]["full_text"]
            if item["conversation"]["sample_type"] == "multi_turn"
            else item["conversation"]["target_query"]
        ),
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


def run_build(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
    logger: logging.Logger | None = None,
) -> BuildOutcome:
    """Run B-E as a contract-bound, stage-checkpointed state machine."""
    run_root.mkdir(parents=True, exist_ok=True)
    active_logger = logger or logging.getLogger(__name__)
    run_contract: JsonObject = {
        "config_version": config.config_version,
        "config": asdict(config),
        "source_snapshot_id": index.metadata.get("source_snapshot_id"),
        "taxonomy_sha256": index.metadata.get("taxonomy_sha256"),
        "canonical_index_version": index.metadata.get("canonical_index_version"),
        "feature_rule_version": index.metadata.get("feature_rule_version"),
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
            "final_membership", "quality_stage2", "screened_export", "legacy_export",
            "batch_report",
        ):
            mark(stage, "completed", restored=True)
        return BuildOutcome(records=restored, summary=completed["summary"])

    materialized: dict[str, JsonObject] = {}
    final_state = load_stage_checkpoint("final_membership", "final_membership")
    if final_state is None:
        dedup_thresholds = {
            "P0": config.sampling.p0.dedup_threshold,
            "P1": config.sampling.p1.dedup_threshold,
            "P2": config.sampling.p2.dedup_threshold,
        }
        branch_states: dict[str, JsonObject] = {}
        missing: list[str] = []
        for set_name in ("P0", "P1", "P2"):
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
                    candidates = _deduplicate(
                        set_name,
                        raw,
                        threshold=dedup_thresholds[set_name],
                    )
                    state = {
                        "set": set_name,
                        "sampled_count": len(locators),
                        "sampled_identities": [
                            locator["source_identity_sha256"] for locator in locators
                        ],
                        "candidates": candidates,
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
            name: list(branch_states[name]["candidates"])
            for name in ("P0", "P1", "P2")
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
            name: _passing(candidate_sets[name], stage1)
            for name in ("P0", "P1", "P2")
        }
        selection_rounds: dict[str, dict[str, int]] = {
            name: {candidate["source_identity_sha256"]: 0 for candidate in values}
            for name, values in accepted.items()
        }
        audits = {
            name: _branch_audit(
                name,
                int(branch_states[name]["sampled_count"]),
                len(candidate_sets[name]),
                candidate_sets[name],
                stage1,
            )
            for name in ("P0", "P1", "P2")
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
            plan_name = f"p1_round_{round_id}_plan"
            plan = checkpoints.load(plan_name)
            completed_rounds = int(
                manifest_stage_value("p1_supplement", "completed_rounds", 0) or 0
            )
            if plan is None and completed_rounds >= round_id:
                raise ValueError(
                    f"manifest 标记 P1 第 {round_id} 轮已完成，但计划检查点缺失"
                )
            if plan is None:
                active_logger.info(
                    "P1 supplement round=%d sampling requested=%d",
                    round_id,
                    sum(requested.values()),
                )
                with CanonicalCandidateIndex(index) as readonly_index:
                    sampler = CandidateSampler(
                        readonly_index, source_path, taxonomy, config.sampling
                    )
                    new_locators = sampler.sample_p1(
                        sum(requested.values()), processed, requested_by_l2=requested
                    )
                new_materialized = _materialize(
                    index, source_path, taxonomy, new_locators, materialized
                )
                new_candidates = _deduplicate(
                    "P1",
                    new_materialized,
                    accepted["P1"],
                    threshold=config.sampling.p1.dedup_threshold,
                )
                plan = {
                    "round": round_id,
                    "requested_by_l2": requested,
                    "estimated_pass_rate_by_l2": rates,
                    "sampled_identities": [
                        item["source_identity_sha256"] for item in new_locators
                    ],
                    "sampled": len(new_locators),
                    "candidates": new_candidates,
                }
                checkpoints.save(plan_name, plan)
            else:
                active_logger.info(
                    "resume checkpoint=%s candidates=%d",
                    plan_name,
                    len(plan["candidates"]),
                )
            processed.update(plan["sampled_identities"])
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
            result_name = f"p1_round_{round_id}_result"
            round_result = checkpoints.load(result_name)
            if round_result is None and completed_rounds >= round_id:
                raise ValueError(
                    f"manifest 标记 P1 第 {round_id} 轮已完成，但结果检查点缺失"
                )
            if round_result is None:
                if new_candidates:
                    round_stage1 = quality_runner.run_stage1(new_candidates)
                    round_pass = _passing(new_candidates, round_stage1)
                else:
                    round_stage1 = {}
                    round_pass = []
                remaining = dict(deficits)
                added: list[JsonObject] = []
                for candidate in round_pass:
                    key = candidate["taxonomy"]["l2_key"]
                    if remaining.get(key, 0) > 0:
                        added.append(candidate)
                        remaining[key] -= 1
                round_summary: JsonObject = {
                    "round": round_id,
                    "requested_by_l2": plan["requested_by_l2"],
                    "estimated_pass_rate_by_l2": plan["estimated_pass_rate_by_l2"],
                    "sampled": int(plan["sampled"]),
                    "after_dedup": len(new_candidates),
                    "stage1_pass": len(round_pass),
                    "admitted": len(added),
                }
                if not plan["sampled_identities"]:
                    round_summary["stop"] = "candidate_exhausted"
                round_result = {
                    "stage1_results": round_stage1,
                    "admitted_identities": [
                        candidate["source_identity_sha256"] for candidate in added
                    ],
                    "summary": round_summary,
                }
                checkpoints.save(result_name, round_result)
            round_stage1 = dict(round_result["stage1_results"])
            stage1.update(round_stage1)
            round_pass = _passing(new_candidates, round_stage1) if new_candidates else []
            supplement_terminal.update(_l2_counts(new_candidates))
            supplement_pass.update(_l2_counts(round_pass))
            admitted_ids = set(round_result["admitted_identities"])
            added = [
                candidate for candidate in new_candidates
                if candidate["source_identity_sha256"] in admitted_ids
            ]
            accepted["P1"].extend(added)
            selection_rounds["P1"].update(
                {candidate["source_identity_sha256"]: round_id for candidate in added}
            )
            supplement_rounds.append(dict(round_result["summary"]))
            active_logger.info(
                "P1 supplement round=%d complete sampled=%d stage1_pass=%d admitted=%d seconds=%.2f",
                round_id,
                int(round_result["summary"]["sampled"]),
                int(round_result["summary"]["stage1_pass"]),
                int(round_result["summary"]["admitted"]),
                time.monotonic() - round_started,
            )
            mark(
                "p1_supplement",
                "running",
                completed_rounds=round_id,
                admitted=len(accepted["P1"]),
            )
            if round_result["summary"].get("stop") == "candidate_exhausted":
                break

        final_state = {
            "sets": {name: accepted[name] for name in ("P0", "P1", "P2")},
            "stage1_results": stage1,
            "selection_rounds": selection_rounds,
            "evaluated_candidates": evaluated_candidates,
            "evaluation_rounds": evaluation_rounds,
            "audits": audits,
            "p1_target_quotas": p1_target_quotas,
            "supplement_rounds": supplement_rounds,
            "initial_p1_stage1_pass_count": initial_p1_stage1_pass_count,
            "initial_p1_admitted_count": initial_p1_admitted_count,
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
    p1_target_quotas = {
        str(key): int(value) for key, value in final_state["p1_target_quotas"].items()
    }
    supplement_rounds = list(final_state["supplement_rounds"])
    initial_p1_stage1_pass_count = int(final_state["initial_p1_stage1_pass_count"])
    initial_p1_admitted_count = int(final_state["initial_p1_admitted_count"])
    for name in ("P0", "P1", "P2"):
        branch_root = run_root / name
        branch_root.mkdir(parents=True, exist_ok=True)
        _merge_candidate_tasks(
            branch_root / "candidate_tasks.jsonl",
            [
                _candidate_task(
                    candidate,
                    name,
                    evaluation_rounds[name][candidate["source_identity_sha256"]],
                )
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

    output: dict[str, tuple[JsonObject, ...]] = {}
    for set_name in ("P0", "P1", "P2"):
        records = tuple(
            build_screened_canonical_record(
                set_name,
                candidate,
                stage1[candidate["source_identity_sha256"]],
                stage2[candidate["source_identity_sha256"]],
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
                "legacy_schema": "labeled_v4",
                "legacy_mapping_version": 1,
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
        "cross_set_overlap": overlap,
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
    for name in ("P0", "P2"):
        audits[name].update(
            {
                "supplement_sampled": 0,
                "supplement_after_dedup": 0,
                "supplement_stage1_pass": 0,
                "supplement_admitted": 0,
                "final": len(output[name]),
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
