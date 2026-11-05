"""Production command-line entrypoint for the current screening pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import time
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..candidate.index import (
    CanonicalIndexHandle,
    ensure_canonical_candidate_index,
    validate_canonical_source,
)
from ..shared.json_io import JsonObject, sha256_file, write_json
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
from ..delivery.validation import (
    ArtifactValidationError,
    collect_and_validate_artifacts,
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
    from .run_context import _load_manifest
    return _load_manifest(run_root / "manifest.json")


def execute_pipeline(context, index, build_config, taxonomy, quality_runner) -> int:
    """Run or resume immutable Stage1 branches, then assemble one checked package."""
    from ..delivery.package import write_package
    from .layout import external_root
    started = begin_invocation(context.run_root, context.disposition)
    result = None
    try:
        if context.disposition == "reused":
            manifest = _read_manifest(context.run_root)
            validate_manifest_inputs(context.run_root, manifest)
            validate_manifest_source(manifest)
            validate_manifest_artifacts(context.run_root, manifest)
            result = 0
            return result
        validate_canonical_source(index.metadata)
        update_manifest(context.run_root, status="running", stages={"stage2": {"status": "running"}})
        build_started = time.monotonic_ns()
        try:
            outcome = run_build(index, build_config, source_path=context.source_path,
                taxonomy=taxonomy, run_root=context.run_root, quality_runner=quality_runner,
                logger=logging.getLogger("screening.v4"))
        finally:
            record_stage_duration(context.run_root, "p012_build", build_started)
        if outcome.errors:
            update_manifest(context.run_root, status="partial_failed" if outcome.records else "failed",
                stages={"stage2": {"status": "incomplete"},
                        "stage1": {"status": "completed" if all((context.run_root / n / "stage1.frozen.jsonl").exists() for n in ("P0","P1","P2")) else "incomplete"}})
            result = 2
            return result
        manifest = _read_manifest(context.run_root)
        export_report = {"labeled_mapping_version": LABELED_MAPPING_VERSION, "sets": manifest["exports"],
                         "total_records": sum(r["records"] for r in manifest["exports"].values()), "lossy_enum_mappings": 0}
        write_json(context.run_root / "reports/export_validation.json", export_report)
        write_package(context.run_root, manifest)
        validate_manifest_inputs(context.run_root, manifest)
        validate_manifest_source(manifest)
        artifacts = collect_and_validate_artifacts(context.run_root)
        update_manifest(context.run_root, status="completed", artifacts=artifacts,
                        stages={"stage1": {"status": "completed"}, "stage2": {"status": "completed"}})
        manifest = _read_manifest(context.run_root)
        manifest["delivery"] = {"status": "completed", "path": str(external_root(context.run_root))}
        write_json(context.run_root / "manifest.json", manifest)
        result = 0
        return result
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        update_manifest(context.run_root, status="failed", stages={"stage2": {"status": "failed", "error_type": type(exc).__name__}})
        raise
    finally:
        finish_invocation(context.run_root, started, completed=result == 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 screening P0/P1/P2 并生成 internal/external 交付目录")
    parser.add_argument("--source", type=Path, default=None, help="覆盖配置中的 cluster JSONL")
    parser.add_argument("--taxonomy", type=Path, default=None, help="覆盖配置中的 taxonomy JSON")
    parser.add_argument("--risk-keywords", type=Path, default=None, help="风险关键词 Excel")
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--new-run", action="store_true")
    parser.add_argument("--stage1-from", type=Path, default=None, help="引用已有完整 Stage1，仅重做 Stage2 并新建 run")
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
    if sum((bool(args.run_id), args.new_run, args.stage1_from is not None)) > 1:
        raise ValueError("--run-id、--new-run、--stage1-from 互斥")
    if args.stage1_from is not None and any((args.admission_prompt, args.category_prompt, args.challenge_prompt)):
        raise ValueError("复用 Stage1 时不得覆盖其准入或 Challenge Prompt")


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_config_override: LlmConfig | None = None,
    llm_call_override: Any | None = None,
    llm_metadata_override: Mapping[str, Any] | None = None,
) -> int:
    context = None
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if any(a.split("=", 1)[0] in {"--publish-root", "--no-publish", "--replace"} for a in arguments):
            print("发布参数已移除；请仅指定 --output-root，交付自动生成于 <run-id>/external/evaluation-set-large")
            return 1
        args = build_parser().parse_args(arguments)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 1
    try:
        _validate_cli(args)
        profile = load_pipeline_config(args.config)
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
            },
        )
        origin = None
        inherited = None
        if args.stage1_from is not None:
            from .reuse import inspect_source, source_overrides
            origin = inspect_source(args.stage1_from)
            effective = apply_pipeline_overrides(effective, source_overrides(origin))
            from dataclasses import replace
            effective = replace(effective, build=origin["build"], screening_policy=origin["stage1_contract"]["screening_policy"])
        if args.run_id and origin is None and (effective.output_root / args.run_id / "internal/manifest.json").is_file():
            resumed = _read_manifest(effective.output_root / args.run_id / "internal")
            if resumed.get("stage1_source"):
                from .reuse import inspect_source, source_overrides
                inherited = inspect_source(effective.output_root / args.run_id)
                effective = apply_pipeline_overrides(effective, source_overrides(inherited))
                from dataclasses import replace
                effective = replace(effective, build=inherited["build"], screening_policy=inherited["stage1_contract"]["screening_policy"])
        configure_logging(effective.log_level)
        logger = logging.getLogger("screening.v4")
        logger.info("screening v4 startup config=%s", effective.config_path)
        effective.validate_for_execution(require_risk_workbook=origin is None and inherited is None)
        assert effective.source is not None and effective.taxonomy is not None and effective.risk_keywords is not None
        from ..shared.risk_keywords import load_risk_keywords
        risk_snapshot = (origin or inherited)["risk_snapshot"] if (origin or inherited) else load_risk_keywords(effective.risk_keywords)
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
            llm_config=llm_config, llm_call=llm_call, logger=logger,
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
            new_run=args.new_run or origin is not None,
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
                stage1_from=None if args.stage1_from is None else str(args.stage1_from),
            ),
        )
        if origin is not None:
            from .reuse import import_stage1
            import_stage1(origin, context, index, build_config)
        elif _read_manifest(context.run_root).get("stage1_source"):
            from .reuse import restore_stage1_identity
            restore_stage1_identity(context)
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
            snapshot_root=context.run_root / "snapshots/prompts",
        )
        if origin is not None:
            from .reuse import reuse_annotations
            reuse_annotations(origin, runner)
        print(f"run_path={context.run_root.parent}\nexternal_path={context.run_root.parent / 'external/evaluation-set-large'}", flush=True)
        result = execute_pipeline(
            context,
            index,
            build_config,
            taxonomy,
            runner,
        )
        print(f"run_path={context.run_root.parent}\nexternal_path={context.run_root.parent / 'external/evaluation-set-large'}")
        final_manifest = _read_manifest(context.run_root)
        print(f"v4 run={context.run_id} status={final_manifest['status']}")
        print("completion_status=" + final_manifest.get("delivery", {}).get("status", "incomplete"))
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
    except (OSError, ValueError, RuntimeError, KeyError, ArtifactValidationError) as exc:
        if context is not None:
            update_manifest(context.run_root, status="failed")
        print(f"screening v4 failed: {exc}")
        print("completion_status=failed")
        return 1
