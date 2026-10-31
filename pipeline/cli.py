"""Production command-line entrypoint for the current screening pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..candidate.index import (
    CanonicalIndexHandle,
    ensure_canonical_candidate_index,
    validate_canonical_source,
)
from ..shared.json_io import JsonObject, sha256_file
from ..shared.taxonomy import TaxonomyMappings, load_taxonomy_mappings
from ..delivery.labeled import LABELED_MAPPING_VERSION
from ..delivery.report import RUN_REPORT_SCHEMA_VERSION
from ..shared.llm import LlmConfig, load_llm_config, validate_llm_dependency
from .orchestrator import BuildConfig, run_build
from .preflight import run_preflight
from ..delivery.screened import SCREENED_CANONICAL_SCHEMA_VERSION
from ..quality.runner import (
    QualityStageIncomplete,
    QualityRunner,
)
from ..delivery.publish import (
    PublicationError,
    collect_and_validate_artifacts,
    publish_labeled,
    publish_labeled_branch,
    validate_manifest_artifacts,
    validate_manifest_inputs,
    validate_manifest_source,
)
from .config import (
    apply_pipeline_overrides,
    configure_logging,
    load_pipeline_config,
)
from .run_context import (
    RunContext,
    begin_invocation,
    finish_invocation,
    prepare_run_context,
    record_stage_duration,
    update_manifest,
    utc_now,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "artifacts" / "config" / "pipeline.default.json"


def _read_manifest(run_root: Path) -> JsonObject:
    value = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("v4 manifest 必须是对象")
    return value


def _complete_or_publish(
    context: RunContext,
    *,
    publish_root: Path | None,
    replace: bool,
) -> int:
    manifest = _read_manifest(context.run_root)
    validate_manifest_inputs(context.run_root, manifest)
    validate_manifest_source(manifest)
    validate_manifest_artifacts(context.run_root, manifest)
    if publish_root is None:
        if manifest.get("status") != "published":
            update_manifest(context.run_root, status="completed")
        return 0
    publish_started_ns = time.monotonic_ns()
    try:
        publication = publish_labeled(
            context.run_root, publish_root, manifest, replace=replace
        )
    except (OSError, ValueError, KeyError) as exc:
        update_manifest(
            context.run_root,
            status="publish_failed",
            stages={"publish": {"status": "failed", "error": str(exc)}},
        )
        raise
    finally:
        record_stage_duration(context.run_root, "publish", publish_started_ns)
    publication["published_at"] = utc_now()
    update_manifest(
        context.run_root,
        status="published",
        stages={"publish": {"status": "completed", "error": None}},
        publication=publication,
    )
    return 0


def _execute_pipeline_impl(
    context: RunContext,
    index: CanonicalIndexHandle,
    build_config: BuildConfig,
    taxonomy: TaxonomyMappings,
    quality_runner: QualityRunner,
    *,
    publish_root: Path | None,
    replace: bool = False,
) -> int:
    """Execute/resume Stage1 collection construction and Stage2 downstream delivery."""
    validate_canonical_source(index.metadata)
    update_manifest(
        context.run_root,
        status="running",
        stages={"p012_build": {"status": "running", "error": None}, "stage1": {"status": "running"}, "stage2": {"status": "pending"}},
    )
    build_started_ns = time.monotonic_ns()
    branch_publication: JsonObject = {}

    def publish_completed_branch(set_name: str, _result: JsonObject) -> None:
        if publish_root is None:
            return
        current_manifest = _read_manifest(context.run_root)
        branch_state = current_manifest.get("branches", {}).get(set_name, {})
        if branch_state.get("status") == "published":
            existing = branch_state.get("publication")
            if isinstance(existing, Mapping):
                branch_publication[set_name] = dict(existing)
            return
        result = publish_labeled_branch(
            context.run_root,
            publish_root,
            current_manifest,
            set_name,
            replace=replace,
        )
        result["published_at"] = utc_now()
        branch_publication[set_name] = result
        update_manifest(
            context.run_root,
            branches={set_name: {
                "status": "published",
                "stage": "published",
                "publication": result,
                "error": None,
            }},
        )

    try:
        outcome = run_build(
            index,
            build_config,
            source_path=context.source_path,
            taxonomy=taxonomy,
            run_root=context.run_root,
            quality_runner=quality_runner,
            logger=logging.getLogger("screening.v4"),
            on_branch_complete=publish_completed_branch,
        )
    except QualityStageIncomplete as exc:
        current_manifest = _read_manifest(context.run_root)
        if exc.stage == "challenge":
            stage_name = "quality_p2_challenge"
        elif current_manifest.get("stages", {}).get("p2_supplement", {}).get("status") == "running":
            stage_name = "p2_supplement"
        elif current_manifest.get("stages", {}).get("p1_supplement", {}).get("status") == "running":
            stage_name = "p1_supplement"
        else:
            stage_name = "quality_admission_initial"
        stages: JsonObject = {
            "p012_build": {"status": "incomplete", "error": str(exc)},
            stage_name: {
                "status": "incomplete",
                "engineering_failure_count": len(exc.failures),
                "engineering_failures": list(exc.failures),
            },
        }
        if exc.stage in {"admission", "admission_features", "admission_category"}:
            stages["quality_admission"] = {
                "status": "incomplete",
                "engineering_failure_count": len(exc.failures),
                "engineering_failures": list(exc.failures),
            }
        update_manifest(
            context.run_root,
            status="incomplete",
            stages=stages,
        )
        return 2
    finally:
        record_stage_duration(context.run_root, "p012_build", build_started_ns)

    if outcome.errors:
        status = "partial_failed" if outcome.records else "failed"
        update_manifest(
            context.run_root,
            status=status,
            stages={
                "stage1": {"status": "completed" if all((context.run_root / name / "stage1.frozen.jsonl").is_file() for name in ("P0", "P1", "P2")) else "incomplete"},
                "stage2": {"status": "incomplete"},
                "p012_build": {
                    "status": status,
                    "failed_branches": dict(outcome.errors),
                    "error": None,
                }
            },
            publication=branch_publication,
        )
        return 2

    artifacts = collect_and_validate_artifacts(context.run_root)
    current_branches = _read_manifest(context.run_root).get("branches", {})
    branch_details = {
        name: {
            "status": current_branches.get(name, {}).get("status", "completed"),
            "stage": current_branches.get(name, {}).get("stage", "completed"),
            "duration_ms": current_branches.get(name, {}).get("duration_ms", 0),
            "summary": outcome.summary["sets"][name],
            "artifacts": current_branches.get(name, {}).get("artifacts", {}),
            "publication": current_branches.get(name, {}).get("publication"),
            "error": None,
        }
        for name in ("P0", "P1", "P2")
    }
    update_manifest(
        context.run_root,
        status="completed",
        stages={
            "stage1": {"status": "completed"},
            "stage2": {"status": "completed"},
            "p012_build": {"status": "completed", "error": None},
            "quality_admission": {"status": "completed", "error": None},
            "p1_supplement": {
                "status": "completed",
                "summary": outcome.summary["p1_supplement"],
                "error": None,
            },
            "p2_supplement": {
                "status": "completed",
                "summary": outcome.summary["p2_adaptive"],
                "error": None,
            },
            "quality_p2_challenge": {"status": "completed", "error": None},
            "screened_export": {
                "status": "completed",
                "screened_schema_version": SCREENED_CANONICAL_SCHEMA_VERSION,
                "error": None,
            },
            "labeled_export": {
                "status": "completed",
                "labeled_mapping_version": LABELED_MAPPING_VERSION,
                "error": None,
            },
            "batch_report": {
                "status": "completed",
                "run_report_schema_version": RUN_REPORT_SCHEMA_VERSION,
                "error": None,
            },
        },
        branches=branch_details,
        artifacts=artifacts,
        publication=branch_publication if branch_publication else None,
    )
    manifest = _read_manifest(context.run_root)
    validate_manifest_artifacts(context.run_root, manifest)
    return _complete_or_publish(context, publish_root=publish_root, replace=replace)


def execute_pipeline(
    context: RunContext,
    index: CanonicalIndexHandle,
    build_config: BuildConfig,
    taxonomy: TaxonomyMappings,
    quality_runner: QualityRunner,
    *,
    publish_root: Path | None,
    replace: bool = False,
) -> int:
    """Measure and execute one resumable v4 invocation."""
    started_ns = begin_invocation(context.run_root, context.disposition)
    result: int | None = None
    try:
        result = _execute_pipeline_impl(
            context,
            index,
            build_config,
            taxonomy,
            quality_runner,
            publish_root=publish_root,
            replace=replace,
        )
        return result
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        manifest = _read_manifest(context.run_root)
        if manifest.get("status") != "publish_failed":
            update_manifest(
                context.run_root,
                status="failed",
                stages={
                    "p012_build": {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                },
            )
        raise
    finally:
        finish_invocation(
            context.run_root,
            started_ns,
            completed=result == 0,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建并发布 screening v4 P0/P1/P2 数据集")
    parser.add_argument("--source", type=Path, default=None, help="覆盖配置中的 cluster JSONL")
    parser.add_argument("--taxonomy", type=Path, default=None, help="覆盖配置中的 taxonomy JSON")
    parser.add_argument("--risk-keywords", type=Path, default=None, help="风险关键词 Excel")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--publish-root", type=Path, default=None)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--new-run", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--admission-prompt", type=Path, default=None)
    parser.add_argument("--category-prompt", type=Path, default=None)
    parser.add_argument("--challenge-prompt", type=Path, default=None)
    parser.add_argument("--quality-annotation-prompt", type=Path, default=None, help="Stage2 下游质量标注 Prompt")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=None)
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--index-batch-size", type=int, default=None)
    parser.add_argument("--index-progress-every", type=int, default=None)
    parser.add_argument("--index-cache-mb", type=int, default=None)
    parser.add_argument("--index-min-free-space-gb", type=float, default=None)
    parser.add_argument("--index-reserve-free-space-gb", type=float, default=None)
    verification = parser.add_mutually_exclusive_group()
    verification.add_argument(
        "--verify-full-source-hash",
        dest="verify_full_source_hash",
        action="store_true",
        default=None,
    )
    verification.add_argument(
        "--no-verify-full-source-hash",
        dest="verify_full_source_hash",
        action="store_false",
    )
    parser.add_argument("--log-level", default=None)
    return parser


def _validate_cli(args: argparse.Namespace) -> None:
    if args.no_publish and args.publish_root is not None:
        raise ValueError("--no-publish 与 --publish-root 不能同时使用")
    if args.replace and args.no_publish:
        raise ValueError("--replace 只能用于发布")
    if args.run_id and args.new_run:
        raise ValueError("--run-id 与 --new-run 不能同时使用")


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_config_override: LlmConfig | None = None,
    llm_call_override: Any | None = None,
    llm_metadata_override: Mapping[str, Any] | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    try:
        _validate_cli(args)
        profile = load_pipeline_config(args.config)
        publication_enabled: bool | None = None
        if args.no_publish:
            publication_enabled = False
        elif args.publish_root is not None:
            publication_enabled = True
        effective = apply_pipeline_overrides(
            profile,
            {
                "source": args.source,
                "taxonomy": args.taxonomy,
                "risk_keywords": args.risk_keywords,
                "output_root": args.output_root,
                "admission_prompt": args.admission_prompt,
                "category_prompt": args.category_prompt,
                "challenge_prompt": args.challenge_prompt,
                "quality_annotation_prompt": args.quality_annotation_prompt,
                "workers": args.workers,
                "max_retries": args.max_retries,
                "sleep_seconds": args.sleep,
                "cache_root": args.cache_root,
                "index_batch_size": args.index_batch_size,
                "index_progress_every": args.index_progress_every,
                "index_cache_mb": args.index_cache_mb,
                "index_min_free_space_gb": args.index_min_free_space_gb,
                "index_reserve_free_space_gb": args.index_reserve_free_space_gb,
                "verify_full_source_hash": args.verify_full_source_hash,
                "log_level": args.log_level,
                "publication_enabled": publication_enabled,
                "publish_root": args.publish_root,
            },
        )
        if args.replace and not effective.publication.enabled:
            raise ValueError("--replace 只能用于发布")
        configure_logging(effective.log_level)
        logger = logging.getLogger("screening.v4")
        logger.info("screening v4 startup config=%s", effective.config_path)
        effective.validate_for_execution()
        assert effective.source is not None and effective.taxonomy is not None and effective.risk_keywords is not None
        from ..shared.risk_keywords import load_risk_keywords
        risk_snapshot = load_risk_keywords(effective.risk_keywords)
        build_config = effective.build
        quality_config = effective.quality
        category_prompt = effective.category_prompt.read_text(encoding="utf-8")
        admission_prompt = effective.admission_prompt.read_text(encoding="utf-8")
        challenge_prompt = effective.challenge_prompt.read_text(encoding="utf-8")
        from ..quality.annotation import stage_definition
        additional_stages = {"quality_annotation": stage_definition(effective.quality_annotation_prompt.read_text(encoding="utf-8"))}
        additional_prompts = {name: definition.prompt for name, definition in additional_stages.items()}
        llm_config = llm_config_override or load_llm_config()
        validate_llm_dependency()
        taxonomy = load_taxonomy_mappings(effective.taxonomy)
        llm_call = llm_call_override or llm_config.bind()
        preflight = run_preflight(
            effective,
            prompts={"admission": admission_prompt, "category": category_prompt, "challenge": challenge_prompt, **additional_prompts},
            llm_config=llm_config, llm_call=llm_call, replace=args.replace, logger=logger,
        )
        taxonomy_sha256 = sha256_file(effective.taxonomy)
        logger.info("canonical index open source_bytes=%d", effective.source.stat().st_size)
        index = ensure_canonical_candidate_index(
            effective.source,
            taxonomy,
            taxonomy_sha256=taxonomy_sha256,
            cache_root=effective.cache_root,
            batch_size=effective.index.batch_size,
            progress_every=effective.index.progress_every,
            min_free_bytes=int(effective.index.min_free_space_gb * 1024**3),
            reserve_free_bytes=int(effective.index.reserve_free_space_gb * 1024**3),
            cache_mb=effective.index.sqlite_cache_mb,
            verify_full_source_hash=effective.index.verify_full_source_hash,
            logger=logger,
        )
        logger.info(
            "canonical index ready candidates=%s cache=%s",
            index.metadata.get("counts", {}).get("candidate_rows"),
            index.cache_dir,
        )
        metadata = dict(llm_metadata_override or llm_config.public_metadata())
        context = prepare_run_context(
            effective.output_root,
            effective.source,
            effective.taxonomy,
            index,
            build_config,
            quality_config,
            admission_prompt=admission_prompt,
            category_prompt=category_prompt,
            screening_policy=effective.screening_policy,
            challenge_prompt=challenge_prompt,
            additional_prompts=additional_prompts,
            llm_metadata=metadata,
            risk_snapshot=risk_snapshot,
            run_id=args.run_id,
            new_run=args.new_run,
        )
        logger.info(
            "run context run_id=%s disposition=%s",
            context.run_id,
            context.disposition,
        )
        update_manifest(
            context.run_root,
            preflight=preflight,
            effective_config=effective.manifest_value(
                run_id=args.run_id,
                new_run=args.new_run,
                replace_files=args.replace,
            ),
        )
        runner = QualityRunner(
            context.run_root / "quality",
            admission_prompt=admission_prompt,
            category_prompt=category_prompt,
            screening_policy=effective.screening_policy,
            challenge_prompt=challenge_prompt,
            additional_stages=additional_stages,
            model=llm_config.model,
            llm_options=llm_config.options(),
            llm_call=llm_call,
            config=quality_config,
            logger=logger,
        )
        result = execute_pipeline(
            context,
            index,
            build_config,
            taxonomy,
            runner,
            publish_root=effective.publish_root,
            replace=args.replace,
        )
        final_manifest = _read_manifest(context.run_root)
        print(f"v4 run={context.run_id} status={final_manifest['status']}")
        if result == 2:
            branch_states = final_manifest.get("branches", {})
            print(
                "branches "
                + " ".join(
                    f"{name}={branch_states.get(name, {}).get('status', 'unknown')}"
                    for name in ("P0", "P1", "P2")
                )
            )
        return result
    except (OSError, ValueError, RuntimeError, KeyError, PublicationError) as exc:
        print(f"screening v4 failed: {exc}")
        return 1
