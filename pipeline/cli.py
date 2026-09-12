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
from ..delivery.legacy import LEGACY_MAPPING_VERSION
from ..delivery.report import RUN_REPORT_SCHEMA_VERSION
from ..shared.llm import LlmConfig, load_llm_config, validate_llm_dependency
from .orchestrator import BuildConfig, load_build_config, run_build
from ..delivery.screened import SCREENED_CANONICAL_SCHEMA_VERSION
from ..quality.two_stage import (
    QualityRunConfig,
    QualityStageIncomplete,
    TwoStageQualityRunner,
)
from ..delivery.publish import (
    PublicationError,
    collect_and_validate_artifacts,
    publish_labeled,
    validate_manifest_artifacts,
    validate_manifest_inputs,
    validate_manifest_source,
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
DEFAULT_OUTPUT_ROOT = PACKAGE_ROOT / "outputs_v2"
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "artifacts" / "config" / "pipeline.default.json"
DEFAULT_STAGE1_PROMPT = PACKAGE_ROOT / "artifacts" / "prompts" / "quality_stage1.md"
DEFAULT_STAGE2_PROMPT = PACKAGE_ROOT / "artifacts" / "prompts" / "quality_stage2.md"


def _read_manifest(run_root: Path) -> JsonObject:
    value = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("v2 manifest 必须是对象")
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
    quality_runner: TwoStageQualityRunner,
    *,
    publish_root: Path | None,
    replace: bool = False,
) -> int:
    """Execute or resume A-E, then optionally publish the three labeled files."""
    manifest = _read_manifest(context.run_root)
    export_status = manifest.get("stages", {}).get("screened_export", {}).get("status")
    report_status = manifest.get("stages", {}).get("batch_report", {}).get("status")
    if (
        export_status == "completed"
        and report_status == "completed"
        and manifest.get("artifacts")
    ):
        return _complete_or_publish(context, publish_root=publish_root, replace=replace)

    validate_canonical_source(index.metadata)
    update_manifest(
        context.run_root,
        status="running",
        stages={"p012_build": {"status": "running", "error": None}},
        branches={name: {"status": "running", "error": None} for name in ("P0", "P1", "P2")},
    )
    build_started_ns = time.monotonic_ns()
    try:
        outcome = run_build(
            index,
            build_config,
            source_path=context.source_path,
            taxonomy=taxonomy,
            run_root=context.run_root,
            quality_runner=quality_runner,
        )
    except QualityStageIncomplete as exc:
        stage_name = "quality_stage1" if exc.stage == "stage1" else "quality_stage2"
        stages: JsonObject = {
            "p012_build": {"status": "incomplete", "error": str(exc)},
            stage_name: {
                "status": "incomplete",
                "engineering_failure_count": len(exc.failures),
                "engineering_failures": list(exc.failures),
            },
        }
        if exc.stage == "stage2":
            stages["quality_stage1"] = {"status": "completed", "error": None}
            stages["p1_supplement"] = {"status": "completed", "error": None}
        update_manifest(
            context.run_root,
            status="incomplete",
            stages=stages,
            branches={name: {"status": "incomplete"} for name in ("P0", "P1", "P2")},
        )
        return 2
    finally:
        record_stage_duration(context.run_root, "p012_build", build_started_ns)

    artifacts = collect_and_validate_artifacts(context.run_root)
    branch_details = {
        name: {"status": "completed", "summary": outcome.summary["sets"][name], "error": None}
        for name in ("P0", "P1", "P2")
    }
    update_manifest(
        context.run_root,
        status="completed",
        stages={
            "p012_build": {"status": "completed", "error": None},
            "quality_stage1": {"status": "completed", "error": None},
            "p1_supplement": {
                "status": "completed",
                "summary": outcome.summary["p1_supplement"],
                "error": None,
            },
            "quality_stage2": {"status": "completed", "error": None},
            "screened_export": {
                "status": "completed",
                "screened_schema_version": SCREENED_CANONICAL_SCHEMA_VERSION,
                "legacy_mapping_version": LEGACY_MAPPING_VERSION,
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
    )
    manifest = _read_manifest(context.run_root)
    validate_manifest_artifacts(context.run_root, manifest)
    return _complete_or_publish(context, publish_root=publish_root, replace=replace)


def execute_pipeline(
    context: RunContext,
    index: CanonicalIndexHandle,
    build_config: BuildConfig,
    taxonomy: TaxonomyMappings,
    quality_runner: TwoStageQualityRunner,
    *,
    publish_root: Path | None,
    replace: bool = False,
) -> int:
    """Measure and execute one resumable v2 invocation."""
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
                branches={
                    name: {"status": "failed", "error_type": type(exc).__name__}
                    for name in ("P0", "P1", "P2")
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
    parser = argparse.ArgumentParser(description="构建并发布 screening v2 P0/P1/P2 数据集")
    parser.add_argument("--source", type=Path, required=True, help="cluster JSONL")
    parser.add_argument("--taxonomy", type=Path, required=True, help="taxonomy JSON")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--publish-root", type=Path, default=None)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--new-run", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--stage1-prompt", type=Path, default=DEFAULT_STAGE1_PROMPT)
    parser.add_argument("--stage2-prompt", type=Path, default=DEFAULT_STAGE2_PROMPT)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--index-batch-size", type=int, default=10_000)
    parser.add_argument("--index-progress-every", type=int, default=100_000)
    parser.add_argument("--index-cache-mb", type=int, default=64)
    parser.add_argument("--index-min-free-space-gb", type=float, default=3.0)
    parser.add_argument("--index-reserve-free-space-gb", type=float, default=1.0)
    parser.add_argument("--verify-full-source-hash", action="store_true")
    return parser


def _validate_cli(args: argparse.Namespace) -> None:
    if args.no_publish and args.publish_root is not None:
        raise ValueError("--no-publish 与 --publish-root 不能同时使用")
    if not args.no_publish and args.publish_root is None:
        raise ValueError("正式发布必须显式提供 --publish-root；仅构建请使用 --no-publish")
    if args.replace and args.no_publish:
        raise ValueError("--replace 只能用于发布")
    if args.run_id and args.new_run:
        raise ValueError("--run-id 与 --new-run 不能同时使用")
    if args.index_min_free_space_gb < args.index_reserve_free_space_gb:
        raise ValueError("index min free space 不能小于 reserve free space")


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
        build_config = load_build_config(args.config)
        quality_config = QualityRunConfig(
            workers=args.workers,
            max_retries=args.max_retries,
            sleep_seconds=args.sleep,
        )
        stage1_prompt = args.stage1_prompt.read_text(encoding="utf-8")
        stage2_prompt = args.stage2_prompt.read_text(encoding="utf-8")
        llm_config = llm_config_override or load_llm_config()
        validate_llm_dependency()
        taxonomy = load_taxonomy_mappings(args.taxonomy)
        taxonomy_sha256 = sha256_file(args.taxonomy)
        cache_root = (args.cache_root or args.output_root / "_canonical_cache").resolve()
        logger = logging.getLogger("screening.v2")
        index = ensure_canonical_candidate_index(
            args.source,
            taxonomy,
            taxonomy_sha256=taxonomy_sha256,
            cache_root=cache_root,
            batch_size=args.index_batch_size,
            progress_every=args.index_progress_every,
            min_free_bytes=int(args.index_min_free_space_gb * 1024**3),
            reserve_free_bytes=int(args.index_reserve_free_space_gb * 1024**3),
            cache_mb=args.index_cache_mb,
            verify_full_source_hash=args.verify_full_source_hash,
            logger=logger,
        )
        metadata = dict(llm_metadata_override or llm_config.public_metadata())
        context = prepare_run_context(
            args.output_root,
            args.source,
            args.taxonomy,
            index,
            build_config,
            quality_config,
            stage1_prompt=stage1_prompt,
            stage2_prompt=stage2_prompt,
            llm_metadata=metadata,
            run_id=args.run_id,
            new_run=args.new_run,
        )
        runner = TwoStageQualityRunner(
            context.run_root / "quality",
            stage1_prompt=stage1_prompt,
            stage2_prompt=stage2_prompt,
            model=llm_config.model,
            llm_options=llm_config.options(),
            llm_call=llm_call_override or llm_config.bind(),
            config=quality_config,
            logger=logger,
        )
        result = execute_pipeline(
            context,
            index,
            build_config,
            taxonomy,
            runner,
            publish_root=None if args.no_publish else args.publish_root,
            replace=args.replace,
        )
        print(f"v2 run={context.run_id} status={_read_manifest(context.run_root)['status']}")
        return result
    except (OSError, ValueError, RuntimeError, KeyError, PublicationError) as exc:
        print(f"screening v2 failed: {exc}")
        return 1
