"""P0/P1/P2 orchestration, admission, P1 supplement, and final construction."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..candidate.index import CanonicalCandidateIndex, CanonicalIndexHandle
from ..shared.json_io import JsonObject, load_jsonl, write_json, write_jsonl
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
        if self.max_rounds < 0:
            raise ValueError("max_rounds 不能为负数")
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


def load_build_config(path: Path) -> BuildConfig:
    value = json.loads(path.read_text(encoding="utf-8"))
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


def run_build(
    index: CanonicalIndexHandle,
    config: BuildConfig,
    *,
    source_path: Path,
    taxonomy: TaxonomyMappings,
    run_root: Path,
    quality_runner: TwoStageQualityRunner,
) -> BuildOutcome:
    """Run isolated B/C/D construction without touching the production entrypoint."""
    run_root.mkdir(parents=True, exist_ok=True)
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
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            set_name: executor.submit(
                _sample_branch, set_name, index, source_path, taxonomy, config.sampling
            )
            for set_name in ("P0", "P1", "P2")
        }
        locator_sets = {set_name: future.result() for set_name, future in futures.items()}

    materialized: dict[str, JsonObject] = {}
    candidate_sets: dict[str, list[JsonObject]] = {}
    dedup_thresholds = {
        "P0": config.sampling.p0.dedup_threshold,
        "P1": config.sampling.p1.dedup_threshold,
        "P2": config.sampling.p2.dedup_threshold,
    }
    for set_name in ("P0", "P1", "P2"):
        raw = _materialize(index, source_path, taxonomy, locator_sets[set_name], materialized)
        candidate_sets[set_name] = _deduplicate(
            set_name, raw, threshold=dedup_thresholds[set_name]
        )
        branch_root = run_root / set_name
        branch_root.mkdir(parents=True, exist_ok=True)
        _merge_candidate_tasks(
            branch_root / "candidate_tasks.jsonl",
            [_candidate_task(candidate, set_name, 0) for candidate in candidate_sets[set_name]],
        )

    initial_all = [candidate for values in candidate_sets.values() for candidate in values]
    evaluated_candidates = {
        set_name: list(candidate_sets[set_name]) for set_name in ("P0", "P1", "P2")
    }
    evaluation_rounds: dict[str, dict[str, int]] = {
        set_name: {
            candidate["source_identity_sha256"]: 0
            for candidate in candidate_sets[set_name]
        }
        for set_name in ("P0", "P1", "P2")
    }
    stage1 = quality_runner.run_stage1(initial_all)
    accepted = {
        set_name: _passing(candidate_sets[set_name], stage1)
        for set_name in ("P0", "P1", "P2")
    }
    selection_rounds: dict[str, dict[str, int]] = {
        set_name: {
            candidate["source_identity_sha256"]: 0
            for candidate in accepted[set_name]
        }
        for set_name in ("P0", "P1", "P2")
    }
    audits = {
        set_name: _branch_audit(
            set_name,
            len(locator_sets[set_name]),
            len(candidate_sets[set_name]),
            candidate_sets[set_name],
            stage1,
        )
        for set_name in ("P0", "P1", "P2")
    }

    with CanonicalCandidateIndex(index) as readonly_index:
        capacities = readonly_index.available_counts("l2")
    p1_target_quotas = allocate_sqrt_quotas(
        taxonomy.l2_population,
        config.sampling.p1.target_n,
        capacities=capacities,
    )
    # Freeze initial P1 at its target per-L2 allocation before supplementing.
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
    processed = {
        locator["source_identity_sha256"] for locator in locator_sets["P1"]
    }
    supplement_rounds: list[JsonObject] = []

    for round_id in range(1, config.p1_supplement.max_rounds + 1):
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
        with CanonicalCandidateIndex(index) as readonly_index:
            sampler = CandidateSampler(
                readonly_index, source_path, taxonomy, config.sampling
            )
            new_locators = sampler.sample_p1(
                sum(requested.values()), processed, requested_by_l2=requested
            )
        processed.update(item["source_identity_sha256"] for item in new_locators)
        if not new_locators:
            supplement_rounds.append(
                {"round": round_id, "requested_by_l2": requested, "sampled": 0, "stop": "candidate_exhausted"}
            )
            break
        new_materialized = _materialize(
            index, source_path, taxonomy, new_locators, materialized
        )
        new_candidates = _deduplicate(
            "P1",
            new_materialized,
            accepted["P1"],
            threshold=config.sampling.p1.dedup_threshold,
        )
        if not new_candidates:
            supplement_rounds.append(
                {"round": round_id, "requested_by_l2": requested, "sampled": len(new_locators), "after_dedup": 0}
            )
            continue
        _merge_candidate_tasks(
            run_root / "P1" / "candidate_tasks.jsonl",
            [_candidate_task(candidate, "P1", round_id) for candidate in new_candidates],
        )
        evaluated_candidates["P1"].extend(new_candidates)
        evaluation_rounds["P1"].update(
            {
                candidate["source_identity_sha256"]: round_id
                for candidate in new_candidates
            }
        )
        round_stage1 = quality_runner.run_stage1(new_candidates)
        stage1.update(round_stage1)
        round_pass = _passing(new_candidates, round_stage1)
        supplement_terminal.update(_l2_counts(new_candidates))
        supplement_pass.update(_l2_counts(round_pass))
        remaining = dict(deficits)
        added: list[JsonObject] = []
        for candidate in round_pass:
            key = candidate["taxonomy"]["l2_key"]
            if remaining.get(key, 0) > 0:
                added.append(candidate)
                remaining[key] -= 1
        accepted["P1"].extend(added)
        selection_rounds["P1"].update(
            {candidate["source_identity_sha256"]: round_id for candidate in added}
        )
        supplement_rounds.append(
            {
                "round": round_id,
                "requested_by_l2": requested,
                "estimated_pass_rate_by_l2": rates,
                "sampled": len(new_locators),
                "after_dedup": len(new_candidates),
                "stage1_pass": len(round_pass),
                "admitted": len(added),
            }
        )

    final_all = [candidate for values in accepted.values() for candidate in values]
    final_stage1 = {
        candidate["source_identity_sha256"]: stage1[candidate["source_identity_sha256"]]
        for candidate in final_all
    }
    stage2 = quality_runner.run_stage2(final_all, final_stage1)

    output: dict[str, tuple[JsonObject, ...]] = {}
    export_reports: dict[str, JsonObject] = {}
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
                stage1_task_id=_quality_task_id(
                    quality_runner, "stage1", candidate
                ),
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
        export_reports[set_name] = export_legacy_branch(set_name, records, branch_root)

    write_json(
        run_root / "legacy_export_report.json",
        {
            "legacy_schema": "labeled_v4",
            "legacy_mapping_version": 1,
            "sets": export_reports,
            "total_records": sum(len(records) for records in output.values()),
            "lossy_enum_mappings": 0,
        },
    )

    final_p1_counts = _l2_counts(accepted["P1"])
    p1_deficits = {
        key: max(0, quota - final_p1_counts.get(key, 0))
        for key, quota in p1_target_quotas.items()
    }
    unallocated_target = max(
        0, config.sampling.p1.target_n - sum(p1_target_quotas.values())
    )
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
        set_name: {record["candidate"]["source_identity_sha256"] for record in records}
        for set_name, records in output.items()
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
            set_name: {
                **audits[set_name],
                "final": len(output[set_name]),
                "target": (
                    None if set_name == "P0"
                    else config.sampling.p1.target_n if set_name == "P1"
                    else config.sampling.p2.target_n
                ),
                "deficit": (
                    0 if set_name == "P0"
                    else config.sampling.p1.target_n - len(output["P1"]) if set_name == "P1"
                    else max(0, config.sampling.p2.target_n - len(output[set_name]))
                ),
            }
            for set_name in ("P0", "P1", "P2")
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
    for set_name in ("P0", "P2"):
        audits[set_name].update(
            {
                "supplement_sampled": 0,
                "supplement_after_dedup": 0,
                "supplement_stage1_pass": 0,
                "supplement_admitted": 0,
                "final": len(output[set_name]),
            }
        )
    for set_name in ("P0", "P1", "P2"):
        write_json(run_root / set_name / "branch_audit.json", audits[set_name])
        summary["sets"][set_name].update(audits[set_name])
    write_json(run_root / "p012_build_summary.json", summary)
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
    return BuildOutcome(records=output, summary=summary)
