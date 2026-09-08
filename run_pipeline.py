"""One-command orchestration for isolated, parallel dataset branches."""

from __future__ import annotations

import argparse
import json
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .build_screened_samples import build_screened_samples
from .cluster_index import ClusterIndexHandle, cleanup_cluster_index
from .core import (
    JsonObject,
    get_decision,
    load_jsonl,
    parse_scores,
    prescreen_results_path,
    run_prescreen_dataset_path,
    run_screened_dataset_path,
    run_supplement_dataset_path,
    set_workspace,
    sha256_file,
    supplement_artifact_path,
    validate_selected_sets,
    write_json,
    write_text,
)
from .generate_supplement_report import generate_report
from .inference import LlmCall
from .llm_client import LlmConfig, load_llm_config, validate_llm_dependency
from .llm_prescreen import run_prescreen
from .publish_screened_samples import (
    publish_screened_samples,
    screened_artifact_metadata,
    validate_authoritative_screened,
)
from .run_context import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_ROOT,
    RunContext,
    cluster_index_handle_from_manifest,
    ensure_context_cluster_index,
    lightweight_preflight,
    load_pipeline_config,
    prepare_run_context,
    record_cluster_index_state,
    snapshot_prompt_path,
    update_branch_manifest,
    update_manifest,
    validate_source_unchanged,
)
from .logging_config import setup_logger
from .supplement_pipeline import (
    PreparedSupplement,
    SupplementOutcome,
    prepare_supplement_inputs,
    run_supplement,
)


@dataclass(frozen=True)
class BranchOutcome:
    set_name: str
    complete: bool
    expected: int
    terminal: int
    keep: int
    review: int
    reject: int
    engineering_failures: tuple[str, ...] = ()
    fatal_error: str | None = None
    supplement: SupplementOutcome | None = None


def _set_nested(target: dict[str, Any], section: str, key: str, value: Any) -> None:
    if value is not None:
        target.setdefault(section, {})[key] = value


def _config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if args.sets is not None:
        _set_nested(overrides, "common", "sets", validate_selected_sets(args.sets))
    _set_nested(overrides, "common", "log_level", args.log_level)
    _set_nested(
        overrides, "inputs", "taxonomy",
        str(args.taxonomy.expanduser().resolve()) if args.taxonomy else None,
    )
    _set_nested(
        overrides, "inputs", "upstream_cluster_population",
        str(args.upstream_cluster_population.expanduser().resolve())
        if args.upstream_cluster_population else None,
    )
    for key in ("workers", "sleep", "max_retries", "limit"):
        _set_nested(overrides, "quality_control", key, getattr(args, f"qc_{key}"))
    _set_nested(
        overrides, "quality_control", "prompt",
        str(args.qc_prompt.expanduser().resolve()) if args.qc_prompt else None,
    )
    if args.p0_scores is not None:
        _set_nested(overrides, "p0", "scores", sorted(args.p0_scores))
    if args.p1_scores is not None:
        _set_nested(overrides, "p1_coverage", "scores", sorted(args.p1_scores))
    for key in (
        "alpha", "target_n", "seed", "max_rounds", "default_pass_rate",
        "pass_rate_prior_strength", "min_pass_rate", "max_pass_rate",
        "safety_factor",
    ):
        _set_nested(overrides, "p1_coverage", key, getattr(args, f"p1_{key}"))
    if args.p1_supplement is not None:
        overrides.setdefault("p1_coverage", {})["supplement"] = {
            "enabled": args.p1_supplement,
            "strategy": "coverage_quota" if args.p1_supplement else "none",
        }
    if args.p2_scores is not None:
        _set_nested(overrides, "p2", "scores", sorted(args.p2_scores))
    _set_nested(overrides, "publication", "enabled", args.publish)
    _set_nested(
        overrides, "large_inputs", "cluster_cache_root",
        str(args.cluster_cache_root.expanduser().resolve())
        if args.cluster_cache_root else None,
    )
    for key in (
        "index_batch_size", "progress_every", "sqlite_cache_mb",
        "min_free_space_gb", "reserve_free_space_gb",
    ):
        _set_nested(overrides, "large_inputs", key, getattr(args, f"cluster_{key}"))
    if args.verify_full_cluster_hash:
        _set_nested(overrides, "large_inputs", "verify_full_cluster_hash", True)
    return overrides


def _stage_logger(context: RunContext, stage: str) -> logging.Logger:
    return setup_logger(
        f"{__name__}.{stage}",
        str(context.config["common"]["log_level"]),
        context.run_root / "logs" / "pipeline.log",
        run_id=context.run_id,
        stage=stage,
    )


def _result_counts(path: Path) -> dict[str, int]:
    results = load_jsonl(path) if path.is_file() else []
    return {
        "terminal": len(results),
        "keep": sum(get_decision(item) == "KEEP" for item in results),
        "review": sum(get_decision(item) == "REVIEW" for item in results),
        "reject": sum(get_decision(item) == "REJECT" for item in results),
    }


def _metadata(path: Path, run_root: Path, *, jsonl: bool = False) -> JsonObject:
    value: JsonObject = {
        "path": path.relative_to(run_root).as_posix(),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }
    if jsonl:
        value["records"] = len(load_jsonl(path))
    return value


def _compact_rounds(rounds: list[JsonObject] | tuple[JsonObject, ...]) -> list[JsonObject]:
    """Keep orchestration state small; business detail lives in the P1 summary."""
    return [
        {
            "round_id": item.get("round_id"),
            "status": item.get("status"),
            "candidates": item.get("candidates", 0),
            "terminal": item.get("terminal", 0),
            "keep": item.get("keep", 0),
            "engineering_failure_count": len(item.get("engineering_failures", [])),
            "error": item.get("error"),
        }
        for item in rounds
    ]


class _GlobalLlmGate:
    """Share one in-flight limit and fatal configuration signal across branches."""

    def __init__(self, maximum: int, call: LlmCall) -> None:
        self._semaphore = threading.BoundedSemaphore(maximum)
        self._fatal = threading.Event()
        self._call = call

    @property
    def fatal(self) -> bool:
        return self._fatal.is_set()

    @staticmethod
    def _is_global_failure(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 425, 429}:
            return True
        text = str(exc).lower()
        return "llm_api_" in text or "缺少配置" in text or "authentication" in text

    def __call__(self, *args: Any, **kwargs: Any) -> tuple[str, float, float]:
        if self._fatal.is_set():
            raise RuntimeError("LLM_API_global_abort")
        with self._semaphore:
            if self._fatal.is_set():
                raise RuntimeError("LLM_API_global_abort")
            try:
                return self._call(*args, **kwargs)
            except Exception as exc:
                if self._is_global_failure(exc):
                    self._fatal.set()
                raise


def _completed_branch(context: RunContext, set_name: str) -> BranchOutcome | None:
    state = context.manifest.get("branches", {}).get(set_name, {})
    if state.get("build", {}).get("status") != "completed":
        return None
    results = prescreen_results_path(context.run_root, set_name)
    screened = run_screened_dataset_path(context.run_root, set_name)
    if not results.is_file() or not screened.is_file():
        return None
    counts = _result_counts(results)
    supplement: SupplementOutcome | None = None
    if set_name == "P1":
        summary_path = supplement_artifact_path(
            context.run_root, "supplement_summary.json"
        )
        if not summary_path.is_file():
            return None
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary, dict) or summary.get("complete") is not True:
            return None
        supplement = SupplementOutcome(
            complete=True,
            total_deficit=int(summary.get("total_deficit", 0)),
            total_filled=int(summary.get("total_filled", 0)),
            engineering_failures=tuple(summary.get("engineering_failures", [])),
            stop_reason=str(summary.get("stop_reason") or "completed"),
            rounds=tuple(summary.get("rounds", [])),
        )
    return BranchOutcome(
        set_name, True, counts["terminal"], counts["terminal"],
        counts["keep"], counts["review"], counts["reject"],
        supplement=supplement,
    )


def _run_branch(
    context: RunContext,
    set_name: str,
    gated_llm: LlmCall,
    model: str,
    llm_options: JsonObject,
    p1_preparation: Future[PreparedSupplement] | None = None,
) -> BranchOutcome:
    completed = _completed_branch(context, set_name)
    if completed:
        return completed
    config = context.config
    qc = config["quality_control"]
    policy_key = {"P0": "p0", "P1": "p1_coverage", "P2": "p2"}[set_name]
    policy = config[policy_key]
    stage_logger = _stage_logger(context, f"{set_name.lower()}_branch")
    expected = terminal = keep = review = reject = 0
    active_branch_stage = "prescreen"
    try:
        update_branch_manifest(context.run_root, set_name, "prescreen", status="running")
        prescreen = run_prescreen(
            context.source_root,
            context.run_root,
            selected_sets=[set_name],
            scores_by_set={set_name: set(policy["scores"])},
            prompt_path=snapshot_prompt_path(context.run_root),
            model=model,
            workers=int(qc["workers"]),
            sleep_sec=float(qc["sleep"]),
            max_retries=int(qc["max_retries"]),
            limit=int(qc["limit"]),
            llm_options=llm_options,
            stage_logger=stage_logger,
            llm_call=gated_llm,
        )
        counts = _result_counts(prescreen_results_path(context.run_root, set_name))
        expected = prescreen.expected
        terminal = counts["terminal"]
        keep = counts["keep"]
        review = counts["review"]
        reject = counts["reject"]
        if not prescreen.complete:
            update_branch_manifest(
                context.run_root, set_name, "prescreen", status="incomplete",
                details={
                    "expected": prescreen.expected, **counts,
                    "engineering_failures": list(prescreen.engineering_failures),
                    "parse_failures": prescreen.parse_failures,
                    "error": prescreen.fatal_error,
                },
            )
            return BranchOutcome(
                set_name, False, prescreen.expected, counts["terminal"],
                counts["keep"], counts["review"], counts["reject"],
                prescreen.engineering_failures, prescreen.fatal_error,
            )
        update_branch_manifest(
            context.run_root, set_name, "prescreen", status="completed",
            details={
                "expected": prescreen.expected, **counts,
                "engineering_failures": [], "error": None,
                "artifacts": {
                    "results": _metadata(
                        prescreen_results_path(context.run_root, set_name),
                        context.run_root, jsonl=True,
                    )
                },
            },
        )

        supplement_outcome: SupplementOutcome | None = None
        if set_name == "P1":
            active_branch_stage = "supplement"
            supplement_config = policy["supplement"]
            if supplement_config["enabled"]:
                update_branch_manifest(context.run_root, "P1", "supplement", status="running")

            def rounds_changed(rounds: list[JsonObject]) -> None:
                if supplement_config["enabled"]:
                    update_branch_manifest(
                        context.run_root, "P1", "supplement", status="running",
                        details={"rounds": _compact_rounds(rounds)},
                    )

            supplement_outcome = run_supplement(
                context.source_root,
                context.run_root,
                taxonomy_path=Path(config["inputs"]["taxonomy"]),
                upstream_path=Path(config["inputs"]["upstream_cluster_population"]),
                prompt_path=snapshot_prompt_path(context.run_root),
                alpha=float(policy["alpha"]),
                target_n=policy["target_n"],
                seed=int(policy["seed"]),
                max_rounds=int(policy["max_rounds"]) if supplement_config["enabled"] else 0,
                default_pass_rate=float(policy["default_pass_rate"]),
                pass_rate_prior_strength=float(policy["pass_rate_prior_strength"]),
                min_pass_rate=float(policy["min_pass_rate"]),
                max_pass_rate=float(policy["max_pass_rate"]),
                safety_factor=float(policy["safety_factor"]),
                model=model,
                workers=int(qc["workers"]),
                sleep_sec=float(qc["sleep"]),
                max_retries=int(qc["max_retries"]),
                llm_options=llm_options,
                stage_logger=stage_logger,
                llm_call=gated_llm,
                round_callback=rounds_changed,
                prepared=p1_preparation.result() if p1_preparation else None,
            )
            summary_path = supplement_artifact_path(context.run_root, "supplement_summary.json")
            supplement_details: JsonObject = {
                "rounds": _compact_rounds(supplement_outcome.rounds),
                "total_deficit": supplement_outcome.total_deficit,
                "total_filled": supplement_outcome.total_filled,
                "stop_reason": supplement_outcome.stop_reason,
                "engineering_failures": list(supplement_outcome.engineering_failures),
                "error": supplement_outcome.fatal_error,
                "summary": _metadata(summary_path, context.run_root),
            }
            if not supplement_outcome.complete:
                update_branch_manifest(
                    context.run_root, "P1", "supplement", status="incomplete",
                    details=supplement_details,
                )
                return BranchOutcome(
                    set_name, False, prescreen.expected, counts["terminal"],
                    counts["keep"], counts["review"], counts["reject"],
                    supplement_outcome.engineering_failures,
                    supplement_outcome.fatal_error, supplement_outcome,
                )
            update_branch_manifest(
                context.run_root, "P1", "supplement",
                status="completed" if supplement_config["enabled"] else "disabled",
                details=supplement_details,
            )

        active_branch_stage = "build"
        update_branch_manifest(context.run_root, set_name, "build", status="running")
        branch_summary = build_screened_samples(
            context.source_root, context.run_root, [set_name], config,
            write_combined_summary=False,
        )
        artifacts: JsonObject = {
            "prescreen": _metadata(
                run_prescreen_dataset_path(context.run_root, set_name),
                context.run_root, jsonl=True,
            ),
            "screened": _metadata(
                run_screened_dataset_path(context.run_root, set_name),
                context.run_root, jsonl=True,
            ),
            "summary": _metadata(
                set_workspace(context.run_root, set_name) / "screening_summary.json",
                context.run_root,
            ),
        }
        if set_name == "P1" and policy["supplement"]["enabled"]:
            artifacts["supplement"] = _metadata(
                run_supplement_dataset_path(context.run_root, set_name),
                context.run_root, jsonl=True,
            )
        if set_name == "P1":
            updated_summary = supplement_artifact_path(
                context.run_root, "supplement_summary.json"
            )
            provenance_path = supplement_artifact_path(
                context.run_root, "screened_provenance.jsonl"
            )
            artifacts["supplement_summary"] = _metadata(
                updated_summary, context.run_root
            )
            artifacts["screened_provenance"] = _metadata(
                provenance_path, context.run_root, jsonl=True
            )
            update_branch_manifest(
                context.run_root,
                "P1",
                "supplement",
                details={"summary": artifacts["supplement_summary"]},
            )
        update_branch_manifest(
            context.run_root, set_name, "build", status="completed",
            details={"artifacts": artifacts, "summary": branch_summary["sets"][set_name], "error": None},
        )
        return BranchOutcome(
            set_name, True, prescreen.expected, counts["terminal"],
            counts["keep"], counts["review"], counts["reject"],
            supplement=supplement_outcome,
        )
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        update_branch_manifest(
            context.run_root, set_name, active_branch_stage, status="incomplete",
            details={"error": str(exc)},
        )
        stage_logger.error("%s 分支失败: %s", set_name, exc)
        return BranchOutcome(
            set_name, False, expected, terminal, keep, review, reject,
            fatal_error=str(exc),
        )


def _combine_branch_summaries(run_root: Path, selected_sets: list[str]) -> JsonObject:
    sets: JsonObject = {}
    for set_name in selected_sets:
        path = set_workspace(run_root, set_name) / "screening_summary.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        sets[set_name] = value["detail"]
    combined = {"schema_version": 3, "sets": sets}
    write_json(run_root / "screening_summary.json", combined)
    return combined


def _cleanup_after_publish(
    context: RunContext,
    handle: ClusterIndexHandle | None,
    active_logger: logging.Logger,
) -> None:
    """Best-effort cleanup that can never turn a successful publish into failure."""
    try:
        cleanup = cleanup_cluster_index(
            handle or cluster_index_handle_from_manifest(context.run_root)
        )
        current = cluster_index_handle_from_manifest(context.run_root)
        record_cluster_index_state(context.run_root, current)
        update_manifest(
            context.run_root,
            stage="cluster_prepare",
            stage_status="completed",
            stage_details={"cleanup_status": cleanup.get("cleanup_status")},
        )
        if cleanup.get("cleanup_status") != "completed":
            active_logger.warning("cluster 索引清理未完成，将保留 cleanup_pending")
    except (OSError, ValueError, KeyError) as exc:
        active_logger.warning("screened 已发布，但 cluster 索引清理记账失败: %s", exc)


def execute_pipeline(
    context: RunContext,
    *,
    publish: bool,
    publish_root: Path | None = None,
    llm_config: LlmConfig | None = None,
    llm_call: LlmCall | None = None,
    verify_full_cluster_hash: bool | None = None,
) -> int:
    """Execute, resume, reuse, or republish a managed run."""
    config = context.config
    if llm_config is not None:
        model = llm_config.model
        llm_options = llm_config.options()
        active_llm = llm_call or llm_config.bind()
    elif llm_call is not None:
        # Explicit call injection is retained for deterministic tests and embedding.
        model = "injected-llm"
        llm_options = {
            "max_tokens": 1,
            "timeout": 1.0,
            "stream": False,
            "enable_thinking": False,
            "skip_special_tokens": False,
        }
        active_llm = llm_call
    else:
        raise RuntimeError("未加载固定 LLM 本地配置")
    strict_cluster_hash = (
        bool(config["large_inputs"]["verify_full_cluster_hash"])
        if verify_full_cluster_hash is None
        else verify_full_cluster_hash
    )
    validate_source_unchanged(
        context.run_root,
        verify_full_cluster_hash=strict_cluster_hash,
    )
    selected_sets = list(config["common"]["sets"])
    qc = config["quality_control"]
    pipeline_logger = _stage_logger(context, "pipeline")
    pipeline_logger.info(
        "run=%s disposition=%s source_id=%s config_id=%s source=%s workspace=%s",
        context.run_id, context.disposition, context.source_id, context.config_id,
        context.source_root, context.run_root,
    )
    if publish and int(qc["limit"]) > 0:
        pipeline_logger.error("quality_control.limit 仅用于调试，启用时禁止发布")
        return 1

    if context.disposition == "reused":
        try:
            validate_authoritative_screened(context.source_root, context.run_root, selected_sets)
            if publish:
                publication = publish_screened_samples(
                    context.source_root, context.run_root, selected_sets,
                    publish_root=publish_root,
                    verify_full_cluster_hash=False,
                )
                update_manifest(
                    context.run_root, status="published", stage="publish",
                    stage_status="published", publication=publication,
                )
                if (
                    "P1" in selected_sets
                    and bool(config["large_inputs"]["cleanup_index_after_publish"])
                ):
                    _cleanup_after_publish(context, None, pipeline_logger)
            pipeline_logger.info("复用已完成 run，不重新调用模型")
            return 0
        except (OSError, ValueError, KeyError) as exc:
            update_manifest(
                context.run_root, status="publish_failed", stage="publish",
                stage_status="publish_failed", stage_details={"error": str(exc)},
            )
            pipeline_logger.error("复用或重新发布失败: %s", exc)
            return 1

    try:
        active_stage = "quality_control"
        cluster_handle: ClusterIndexHandle | None = None
        p1_needs_work = (
            "P1" in selected_sets and _completed_branch(context, "P1") is None
        )
        if p1_needs_work:
            active_stage = "cluster_prepare"
            update_manifest(
                context.run_root, status="running", stage="cluster_prepare",
                stage_status="running", stage_details={"error": None},
            )
            cluster_handle = ensure_context_cluster_index(context)
            update_manifest(
                context.run_root, stage="cluster_prepare", stage_status="completed",
                stage_details={
                    "cluster_stat_id": cluster_handle.metadata["cluster_stat_id"] if cluster_handle else None,
                    "sha256": cluster_handle.metadata["sha256"] if cluster_handle else None,
                    "index_version": cluster_handle.metadata["index_version"] if cluster_handle else None,
                    "error": None,
                },
            )
        active_stage = "quality_control"
        update_manifest(
            context.run_root, status="running", stage="quality_control",
            stage_status="running", stage_details={"error": None},
        )
        gate = _GlobalLlmGate(int(qc["workers"]), active_llm)
        outcomes: dict[str, BranchOutcome] = {}
        preparation_future: Future[PreparedSupplement] | None = None
        worker_count = len(selected_sets) + (1 if p1_needs_work else 0)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            if p1_needs_work:
                preparation_future = executor.submit(
                    prepare_supplement_inputs,
                    context.source_root,
                    context.run_root,
                    Path(config["inputs"]["taxonomy"]),
                    Path(config["inputs"]["upstream_cluster_population"]),
                    index_handle=cluster_handle,
                    seed=int(config["p1_coverage"]["seed"]),
                )
            futures = {
                executor.submit(
                    _run_branch,
                    context,
                    set_name,
                    gate,
                    model,
                    llm_options,
                    preparation_future if set_name == "P1" else None,
                ): set_name
                for set_name in selected_sets
            }
            for future in as_completed(futures):
                result = future.result()
                outcomes[result.set_name] = result

        expected = sum(item.expected for item in outcomes.values())
        terminal = sum(item.terminal for item in outcomes.values())
        branch_manifest = json.loads(
            (context.run_root / "manifest.json").read_text(encoding="utf-8")
        )
        prescreen_failures = [
            failure
            for set_name in selected_sets
            for failure in branch_manifest.get("branches", {})
            .get(set_name, {})
            .get("prescreen", {})
            .get("engineering_failures", [])
        ]
        failures = [failure for item in outcomes.values() for failure in item.engineering_failures]
        incomplete = [name for name in selected_sets if not outcomes[name].complete]
        prescreen_complete = all(
            branch_manifest.get("branches", {}).get(name, {}).get("prescreen", {}).get("status") == "completed"
            for name in selected_sets
        )
        qc_status = "completed" if prescreen_complete else "incomplete"
        update_manifest(
            context.run_root,
            status="incomplete" if incomplete else "running",
            stage="quality_control", stage_status=qc_status,
            stage_details={
                "expected": expected, "terminal": terminal,
                "keep": sum(item.keep for item in outcomes.values()),
                "review": sum(item.review for item in outcomes.values()),
                "reject": sum(item.reject for item in outcomes.values()),
                "engineering_failures": prescreen_failures,
                "error": next((item.fatal_error for item in outcomes.values() if item.fatal_error), None),
            },
        )
        if "P1" in selected_sets:
            p1 = outcomes["P1"].supplement
            update_manifest(
                context.run_root, stage="p1_coverage",
                stage_status="completed" if p1 and p1.complete else "incomplete",
                stage_details={
                    "total_deficit": p1.total_deficit if p1 else None,
                    "total_filled": p1.total_filled if p1 else None,
                    "stop_reason": p1.stop_reason if p1 else None,
                    "engineering_failures": list(p1.engineering_failures) if p1 else [],
                    "rounds": _compact_rounds(p1.rounds) if p1 else [],
                },
            )
        else:
            update_manifest(
                context.run_root, stage="p1_coverage", stage_status="completed",
                stage_details={"enabled": False, "stop_reason": "P1_not_selected"},
            )
        if incomplete:
            pipeline_logger.error("未完成分支: %s", ", ".join(incomplete))
            return 2 if failures and not gate.fatal else 1

        active_stage = "build_screened"
        combined = _combine_branch_summaries(context.run_root, selected_sets)
        authoritative = screened_artifact_metadata(
            context.source_root, context.run_root, selected_sets
        )
        update_manifest(
            context.run_root, stage="build_screened", stage_status="completed",
            stage_details={
                "files": authoritative,
                "summary": _metadata(context.run_root / "screening_summary.json", context.run_root),
                "sets": {name: combined["sets"][name]["final_records"] for name in selected_sets},
                "error": None,
            },
        )

        active_stage = "report"
        report = generate_report(context.run_root)
        report_path = context.run_root / "supplement_report.md"
        write_text(report_path, report)
        update_manifest(
            context.run_root, status="completed", stage="report", stage_status="completed",
            stage_details={"report": _metadata(report_path, context.run_root), "error": None},
        )
        if publish:
            active_stage = "publish"
            publication = publish_screened_samples(
                context.source_root, context.run_root, selected_sets,
                publish_root=publish_root,
                verify_full_cluster_hash=False,
            )
            update_manifest(
                context.run_root, status="published", stage="publish",
                stage_status="published", publication=publication,
            )
            pipeline_logger.info("screened 数据集发布完成")
            if (
                "P1" in selected_sets
                and bool(config["large_inputs"]["cleanup_index_after_publish"])
            ):
                _cleanup_after_publish(context, cluster_handle, pipeline_logger)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        failure_status = "publish_failed" if active_stage == "publish" else "incomplete"
        update_manifest(
            context.run_root,
            status=failure_status,
            stage=active_stage,
            stage_status=failure_status,
            stage_details={"error": str(exc)},
        )
        pipeline_logger.error("链路停止: %s", exc)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="一键执行分集合筛选与 P1 Coverage 链路")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--publish-root", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--sets", default=None)
    parser.add_argument("--taxonomy", type=Path, default=None)
    parser.add_argument("--upstream-cluster-population", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--new-run", action="store_true")
    parser.add_argument("--publish", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅轻量检查路径、配置、taxonomy 和 cluster 头部；不创建 run",
    )
    parser.add_argument(
        "--prepare-only", action="store_true",
        help="完整建立 cluster 索引和严格预检，不调用模型、不发布",
    )
    parser.add_argument("--cluster-cache-root", type=Path, default=None)
    parser.add_argument("--cluster-index-batch-size", type=int, default=None)
    parser.add_argument("--cluster-progress-every", type=int, default=None)
    parser.add_argument("--cluster-sqlite-cache-mb", type=int, default=None)
    parser.add_argument("--cluster-min-free-space-gb", type=float, default=None)
    parser.add_argument("--cluster-reserve-free-space-gb", type=float, default=None)
    parser.add_argument(
        "--verify-full-cluster-hash", action="store_true", default=None
    )
    parser.add_argument("--qc-prompt", type=Path, default=None)
    parser.add_argument("--qc-workers", type=int, default=None)
    parser.add_argument("--qc-sleep", type=float, default=None)
    parser.add_argument("--qc-max-retries", type=int, default=None)
    parser.add_argument("--qc-limit", type=int, default=None)
    parser.add_argument("--p0-scores", type=parse_scores, default=None)
    parser.add_argument("--p1-scores", type=parse_scores, default=None)
    parser.add_argument("--p1-alpha", type=float, default=None)
    parser.add_argument("--p1-target-n", type=int, default=None)
    parser.add_argument("--p1-seed", type=int, default=None)
    parser.add_argument("--p1-max-rounds", type=int, default=None)
    parser.add_argument("--p1-default-pass-rate", type=float, default=None)
    parser.add_argument("--p1-pass-rate-prior-strength", type=float, default=None)
    parser.add_argument("--p1-min-pass-rate", type=float, default=None)
    parser.add_argument("--p1-max-pass-rate", type=float, default=None)
    parser.add_argument("--p1-safety-factor", type=float, default=None)
    parser.add_argument("--p1-supplement", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--p2-scores", type=parse_scores, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.dry_run and args.prepare_only:
            raise ValueError("--dry-run 与 --prepare-only 不能同时使用")
        if args.dry_run and (args.run_id or args.new_run):
            raise ValueError("轻量 --dry-run 不创建或选择 run，不能搭配 --run-id/--new-run")
        config, _ = load_pipeline_config(args.config, _config_overrides(args))
        if args.dry_run:
            result = lightweight_preflight(args.source_root, args.output_root, config)
            print(
                "lightweight preflight ok: "
                f"sets={','.join(result['selected_sets'])}; "
                "cluster full scan/index NOT performed"
            )
            return 0
        llm_config = load_llm_config()
        if not args.prepare_only:
            validate_llm_dependency()
        context = prepare_run_context(
            args.source_root, args.output_root, config,
            run_id=args.run_id, new_run=args.new_run,
            llm_metadata=llm_config.public_metadata(),
        )
        if args.prepare_only:
            handle = ensure_context_cluster_index(context)
            update_manifest(
                context.run_root,
                status="created",
                stage="cluster_prepare",
                stage_status="completed" if handle else "disabled",
                stage_details={
                    "cluster_stat_id": handle.metadata["cluster_stat_id"] if handle else None,
                    "sha256": handle.metadata["sha256"] if handle else None,
                    "index_version": handle.metadata["index_version"] if handle else None,
                    "error": None,
                },
            )
            print(
                f"prepare complete: source_id={context.source_id}, "
                f"config_id={context.config_id}, run_root={context.run_root}"
            )
            return 0
        return execute_pipeline(
            context,
            publish=bool(config["publication"]["enabled"]),
            publish_root=args.publish_root,
            llm_config=llm_config,
            verify_full_cluster_hash=args.verify_full_cluster_hash,
        )
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"pipeline initialization failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
