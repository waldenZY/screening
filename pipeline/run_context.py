"""Versioned run identity, frozen contracts, manifests, and recovery."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..candidate.contract import CANONICAL_SCHEMA_VERSION, FEATURE_RULE_VERSION
from ..candidate.index import CANONICAL_INDEX_VERSION, CanonicalIndexHandle
from ..candidate.dedup import RULES_SHA256, load_or_build_idf_snapshot, validate_idf_snapshot
from ..candidate.p2_adaptive import P2_ALGORITHM
from ..shared.json_io import JsonObject, sha256_file, stable_hash, write_json
from ..shared.risk_keywords import validate_risk_snapshot
from ..delivery.legacy import LEGACY_MAPPING_VERSION, LEGACY_SCHEMA_NAME
from ..delivery.report import RUN_REPORT_SCHEMA_VERSION
from .orchestrator import BUILD_CONFIG_VERSION, BuildConfig
from ..delivery.screened import SCREENED_CANONICAL_SCHEMA_VERSION
from ..quality.two_stage import QualityRunConfig
from ..quality.two_stage import QUALITY_CONTRACT_VERSION
from .checkpoints import CHECKPOINT_SCHEMA_VERSION


PIPELINE_VERSION = 3
MANIFEST_SCHEMA_VERSION = 3
_LOCK = threading.RLock()
_RESUMABLE = {"created", "running", "incomplete", "publish_failed"}
_REUSABLE = {"completed", "published"}


@dataclass(frozen=True)
class RunContext:
    run_root: Path
    output_root: Path
    source_path: Path
    taxonomy_path: Path
    run_id: str
    config_id: str
    disposition: str
    manifest: JsonObject


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _prompt_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_run_contract(
    index: CanonicalIndexHandle,
    taxonomy_path: Path,
    build_config: BuildConfig,
    quality_config: QualityRunConfig,
    *,
    stage1_prompt: str,
    challenge_prompt: str,
    stage2_prompt: str,
    llm_metadata: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    idf_snapshot: Mapping[str, Any],
) -> JsonObject:
    taxonomy_path = taxonomy_path.expanduser().resolve()
    return {
        "pipeline_version": PIPELINE_VERSION,
        "source_snapshot_id": index.metadata["source_snapshot_id"],
        "source_stat": index.metadata["source"],
        "canonical_cache_id": index.cache_dir.name,
        "taxonomy_sha256": sha256_file(taxonomy_path),
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "canonical_index_version": CANONICAL_INDEX_VERSION,
        "feature_rule_version": FEATURE_RULE_VERSION,
        "p012_build_config_version": BUILD_CONFIG_VERSION,
        "p2_algorithm": P2_ALGORITHM,
        "screened_canonical_schema_version": SCREENED_CANONICAL_SCHEMA_VERSION,
        "legacy_schema": LEGACY_SCHEMA_NAME,
        "legacy_mapping_version": LEGACY_MAPPING_VERSION,
        "run_report_schema_version": RUN_REPORT_SCHEMA_VERSION,
        "quality_contract_version": QUALITY_CONTRACT_VERSION,
        "risk_keywords_source_sha256": risk_snapshot["source_sha256"],
        "risk_keywords_rules_sha256": risk_snapshot["rules_sha256"],
        "dedup_rules_sha256": RULES_SHA256,
        "dedup_idf_sha256": idf_snapshot["content_sha256"],
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "build_config": asdict(build_config),
        "quality_run_config": asdict(quality_config),
        "prompts": {
            "stage1_sha256": _prompt_hash(stage1_prompt),
            "challenge_sha256": _prompt_hash(challenge_prompt),
            "stage2_sha256": _prompt_hash(stage2_prompt),
        },
        "llm": dict(llm_metadata),
    }


def _load_manifest(path: Path) -> JsonObject:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("v3 manifest 必须是 JSON 对象")
    return value


def _new_run_id(config_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}__{config_id[:12]}"


def _validate_frozen_run(run_root: Path, manifest: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("run manifest schema 与 v3 不兼容")
    if manifest.get("pipeline_version") != PIPELINE_VERSION:
        raise ValueError("run pipeline version 与 v3 不兼容")
    if manifest.get("contract") != dict(contract):
        raise ValueError("run 与当前输入、配置、Prompt 或模型不兼容")
    for stage in ("stage1", "challenge", "stage2"):
        path = run_root / "prompts" / f"{stage}.md"
        expected = contract["prompts"][f"{stage}_sha256"]
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"冻结的 {stage} Prompt 缺失或已变化")


def prepare_run_context(
    output_root: Path,
    source_path: Path,
    taxonomy_path: Path,
    index: CanonicalIndexHandle,
    build_config: BuildConfig,
    quality_config: QualityRunConfig,
    *,
    stage1_prompt: str,
    challenge_prompt: str,
    stage2_prompt: str,
    llm_metadata: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    idf_snapshot: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    new_run: bool = False,
) -> RunContext:
    if run_id and new_run:
        raise ValueError("--run-id 与 --new-run 不能同时使用")
    validate_risk_snapshot(risk_snapshot)
    output_root = output_root.expanduser().resolve()
    source_path = source_path.expanduser().resolve()
    idf_snapshot = dict(idf_snapshot) if idf_snapshot is not None else load_or_build_idf_snapshot(index, source_path)
    validate_idf_snapshot(idf_snapshot, index)
    taxonomy_path = taxonomy_path.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    contract = build_run_contract(
        index,
        taxonomy_path,
        build_config,
        quality_config,
        stage1_prompt=stage1_prompt,
        challenge_prompt=challenge_prompt,
        stage2_prompt=stage2_prompt,
        llm_metadata=llm_metadata,
        risk_snapshot=risk_snapshot,
        idf_snapshot=idf_snapshot,
    )
    config_id = stable_hash(contract)
    runs_root = output_root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    selected: tuple[Path, JsonObject] | None = None
    disposition = "created"

    if run_id:
        if not re.fullmatch(r"[0-9A-Za-z._-]+", run_id):
            raise ValueError("run ID 非法")
        path = runs_root / run_id / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(f"找不到 v3 run: {run_id}")
        manifest = _load_manifest(path)
        _validate_frozen_run(path.parent, manifest, contract)
        selected = (path.parent, manifest)
        disposition = "reused" if manifest.get("status") in _REUSABLE else "resumed"
    elif not new_run:
        candidates: list[tuple[Path, JsonObject]] = []
        for path in runs_root.glob("*/manifest.json"):
            try:
                manifest = _load_manifest(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if (
                manifest.get("pipeline_version") == PIPELINE_VERSION
                and manifest.get("config_id") == config_id
                and manifest.get("contract") == contract
            ):
                candidates.append((path.parent, manifest))
        candidates.sort(key=lambda item: str(item[1].get("created_at", "")), reverse=True)
        resumable = [item for item in candidates if item[1].get("status") in _RESUMABLE]
        reusable = [item for item in candidates if item[1].get("status") in _REUSABLE]
        if resumable:
            selected, disposition = resumable[0], "resumed"
        elif reusable:
            selected, disposition = reusable[0], "reused"

    if selected is None:
        active_run_id = _new_run_id(config_id)
        run_root = runs_root / active_run_id
        run_root.mkdir(parents=True, exist_ok=False)
        prompt_root = run_root / "prompts"
        prompt_root.mkdir()
        (prompt_root / "stage1.md").write_text(stage1_prompt, encoding="utf-8", newline="\n")
        (prompt_root / "challenge.md").write_text(challenge_prompt, encoding="utf-8", newline="\n")
        (prompt_root / "stage2.md").write_text(stage2_prompt, encoding="utf-8", newline="\n")
        write_json(run_root / "risk_keywords.json", dict(risk_snapshot))
        write_json(run_root / "dedup_idf.json", dict(idf_snapshot))
        now = utc_now()
        manifest: JsonObject = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "run_id": active_run_id,
            "config_id": config_id,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "source": {
                "path": str(source_path),
                "sha256": index.metadata["source_snapshot_id"],
            },
            "taxonomy": {
                "path": str(taxonomy_path),
                "sha256": sha256_file(taxonomy_path),
            },
            "index": {
                "cache_dir": str(index.cache_dir),
                "database_path": str(index.database_path),
                "metadata_path": str(index.metadata_path),
                "metadata_sha256": sha256_file(index.metadata_path),
                "counts": index.metadata.get("counts", {}),
            },
            "contract": contract,
            "stages": {
                "canonical_index": {"status": "completed"},
                "sampling_p0": {"status": "pending"},
                "sampling_p1": {"status": "pending"},
                "sampling_p2": {"status": "pending"},
                "quality_stage1_initial": {"status": "pending"},
                "p012_build": {"status": "pending"},
                "quality_stage1": {"status": "pending"},
                "p1_supplement": {"status": "pending"},
                "p2_supplement": {"status": "pending"},
                "quality_p2_challenge": {"status": "pending"},
                "final_membership": {"status": "pending"},
                "quality_stage2": {"status": "pending"},
                "screened_export": {"status": "pending"},
                "legacy_export": {"status": "pending"},
                "batch_report": {"status": "pending"},
                "publish": {"status": "pending"},
            },
            "branches": {name: {"status": "pending"} for name in ("P0", "P1", "P2")},
            "artifacts": {},
            "publication": {},
            "timing": {
                "first_started_at": None,
                "last_started_at": None,
                "last_finished_at": None,
                "completed_at": None,
                "invocation_count": 0,
                "resume_count": 0,
                "active_duration_ms": 0,
                "wall_clock_duration_ms": 0,
            },
        }
        write_json(run_root / "manifest.json", manifest)
        selected = (run_root, manifest)

    selected_root, manifest = selected
    _validate_frozen_run(selected_root, manifest, contract)
    frozen_risk = json.loads((selected_root / "risk_keywords.json").read_text(encoding="utf-8"))
    frozen_idf = json.loads((selected_root / "dedup_idf.json").read_text(encoding="utf-8"))
    validate_idf_snapshot(frozen_idf, index)
    if frozen_idf != dict(idf_snapshot):
        raise ValueError("Frozen dedup IDF differs from current source")
    validate_risk_snapshot(frozen_risk)
    if frozen_risk != dict(risk_snapshot):
        raise ValueError("冻结的风险词表与当前输入不一致")
    return RunContext(
        run_root=selected_root,
        output_root=output_root,
        source_path=source_path,
        taxonomy_path=taxonomy_path,
        run_id=str(manifest["run_id"]),
        config_id=config_id,
        disposition=disposition,
        manifest=dict(manifest),
    )


def update_manifest(
    run_root: Path,
    *,
    status: str | None = None,
    stages: Mapping[str, Mapping[str, Any]] | None = None,
    branches: Mapping[str, Mapping[str, Any]] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    publication: Mapping[str, Any] | None = None,
    effective_config: Mapping[str, Any] | None = None,
) -> JsonObject:
    with _LOCK:
        path = run_root / "manifest.json"
        manifest = _load_manifest(path)
        if status is not None:
            manifest["status"] = status
        for name, details in (stages or {}).items():
            manifest.setdefault("stages", {}).setdefault(name, {}).update(dict(details))
        for name, details in (branches or {}).items():
            manifest.setdefault("branches", {}).setdefault(name, {}).update(dict(details))
        if artifacts is not None:
            manifest["artifacts"] = dict(artifacts)
        if publication is not None:
            manifest["publication"] = dict(publication)
        if effective_config is not None:
            manifest["effective_config"] = dict(effective_config)
        manifest["updated_at"] = utc_now()
        write_json(path, manifest)
        return manifest


def begin_invocation(run_root: Path, disposition: str) -> int:
    """Start one active execution interval and return its monotonic clock."""
    with _LOCK:
        path = run_root / "manifest.json"
        manifest = _load_manifest(path)
        timing = manifest.setdefault("timing", {})
        now = utc_now()
        if timing.get("first_started_at") is None:
            timing["first_started_at"] = now
        timing["last_started_at"] = now
        timing["invocation_count"] = int(timing.get("invocation_count", 0)) + 1
        if disposition == "resumed":
            timing["resume_count"] = int(timing.get("resume_count", 0)) + 1
        manifest["updated_at"] = now
        write_json(path, manifest)
    return time.monotonic_ns()


def finish_invocation(run_root: Path, started_ns: int, *, completed: bool) -> None:
    with _LOCK:
        path = run_root / "manifest.json"
        manifest = _load_manifest(path)
        timing = manifest.setdefault("timing", {})
        now = utc_now()
        duration = max(0, round((time.monotonic_ns() - started_ns) / 1_000_000))
        timing["last_finished_at"] = now
        timing["active_duration_ms"] = int(timing.get("active_duration_ms", 0)) + duration
        first = timing.get("first_started_at")
        if isinstance(first, str):
            try:
                timing["wall_clock_duration_ms"] = max(
                    0,
                    round(
                        (datetime.fromisoformat(now) - datetime.fromisoformat(first)).total_seconds()
                        * 1000
                    ),
                )
            except ValueError:
                timing["wall_clock_duration_ms"] = 0
        if completed:
            timing["completed_at"] = now
        manifest["updated_at"] = now
        write_json(path, manifest)


def record_stage_duration(run_root: Path, stage: str, started_ns: int) -> None:
    with _LOCK:
        path = run_root / "manifest.json"
        manifest = _load_manifest(path)
        stage_entry = manifest.setdefault("stages", {}).setdefault(stage, {})
        timing = stage_entry.setdefault("timing", {})
        duration = max(0, round((time.monotonic_ns() - started_ns) / 1_000_000))
        timing["active_duration_ms"] = int(timing.get("active_duration_ms", 0)) + duration
        timing["execution_count"] = int(timing.get("execution_count", 0)) + 1
        timing["last_finished_at"] = utc_now()
        manifest["updated_at"] = utc_now()
        write_json(path, manifest)
