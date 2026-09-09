"""Managed run directories, configuration, fingerprints, and manifests."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import (
    PRESCREEN_FILES,
    SCREENED_FILES,
    SET_FILES,
    SUPPLEMENT_FILES,
    JsonObject,
    build_candidate,
    ensure_within,
    iter_jsonl,
    load_prompt_template,
    sample_identity,
    sha256_file,
    stable_hash,
    write_json,
)
from .cluster_index import (
    ClusterIndexHandle,
    INPUT_AUDIT_NAME,
    ensure_cluster_index,
    validate_cluster_source,
)
from .coverage import load_taxonomy_mappings

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = PACKAGE_DIR / "outputs"
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "artifacts" / "config" / "pipeline.default.json"
PIPELINE_VERSION = 7
_MANIFEST_LOCK = threading.RLock()

INCOMPLETE_STATUSES = {"created", "running", "incomplete", "publish_failed"}
COMPLETE_STATUSES = {"completed", "published"}


@dataclass(frozen=True)
class RunContext:
    source_root: Path
    output_root: Path
    dataset_root: Path
    run_root: Path
    run_id: str
    source_id: str
    config_id: str
    config: JsonObject
    manifest: JsonObject
    disposition: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _elapsed_ms(started_ns: int) -> int:
    return max(0, round((time.monotonic_ns() - started_ns) / 1_000_000))


def _merge_timing_entry(
    entry: JsonObject,
    *,
    started_at: str,
    finished_at: str,
    duration_ms: int,
) -> None:
    duration = max(0, int(duration_ms))
    entry.setdefault("first_started_at", started_at)
    entry["last_started_at"] = started_at
    entry["last_finished_at"] = finished_at
    entry["last_duration_ms"] = duration
    entry["active_duration_ms"] = int(entry.get("active_duration_ms", 0)) + duration
    entry["execution_count"] = int(entry.get("execution_count", 0)) + 1


def _timing_target(manifest: JsonObject, scope: tuple[str, ...]) -> JsonObject:
    current = manifest
    for key in scope:
        value = current.setdefault(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"manifest 计时路径非法: {'.'.join(scope)}")
        current = value
    return current


def _wall_clock_duration_ms(started_at: str, finished_at: str) -> int:
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return 0
    return max(0, round((finished - started).total_seconds() * 1000))


def _merge_run_timing(
    manifest: JsonObject,
    *,
    phase: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    phase_duration_ms: int | None = None,
    invocation_disposition: str | None = None,
    completed: bool = False,
) -> None:
    timing = manifest.setdefault("timing", {})
    if not isinstance(timing, dict):
        raise ValueError("manifest.timing 必须是对象")
    timing.setdefault("schema_version", 1)
    timing.setdefault("first_started_at", started_at)
    timing["last_finished_at"] = finished_at
    timing["active_duration_ms"] = int(timing.get("active_duration_ms", 0)) + max(
        0, int(duration_ms)
    )
    timing["wall_clock_duration_ms"] = _wall_clock_duration_ms(
        str(timing["first_started_at"]), finished_at
    )
    phases = timing.setdefault("phases", {})
    if not isinstance(phases, dict):
        raise ValueError("manifest.timing.phases 必须是对象")
    phase_entry = phases.setdefault(phase, {})
    if not isinstance(phase_entry, dict):
        raise ValueError(f"manifest.timing.phases.{phase} 必须是对象")
    _merge_timing_entry(
        phase_entry,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms if phase_duration_ms is None else phase_duration_ms,
    )
    if invocation_disposition is not None:
        timing["invocation_count"] = int(timing.get("invocation_count", 0)) + 1
        if invocation_disposition == "resumed":
            timing["resume_count"] = int(timing.get("resume_count", 0)) + 1
        else:
            timing.setdefault("resume_count", 0)
    if completed:
        timing["completed_at"] = finished_at


def record_run_execution_timing(
    run_root: Path,
    *,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    completed: bool,
) -> JsonObject:
    """Accumulate one pipeline execution without counting offline resume gaps."""
    with _MANIFEST_LOCK:
        manifest_path = run_root / "manifest.json"
        manifest = _read_manifest(manifest_path)
        _merge_run_timing(
            manifest,
            phase="pipeline_execution",
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            completed=completed,
        )
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest)
        return manifest


def record_manifest_timing(
    run_root: Path,
    scopes: list[tuple[str, ...]],
    *,
    started_at: str,
    finished_at: str,
    duration_ms: int,
) -> JsonObject:
    """Accumulate one completed active interval into one or more timing scopes."""
    with _MANIFEST_LOCK:
        manifest_path = run_root / "manifest.json"
        manifest = _read_manifest(manifest_path)
        for scope in scopes:
            _merge_timing_entry(
                _timing_target(manifest, scope),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            )
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest)
        return manifest


@contextmanager
def measure_manifest_timing(
    run_root: Path,
    *scopes: tuple[str, ...],
):
    """Record elapsed active time even when the measured operation raises."""
    started_at = utc_now_ms()
    started_ns = time.monotonic_ns()
    try:
        yield
    finally:
        record_manifest_timing(
            run_root,
            list(scopes),
            started_at=started_at,
            finished_at=utc_now_ms(),
            duration_ms=_elapsed_ms(started_ns),
        )


def _deep_merge(base: JsonObject, overlay: JsonObject) -> JsonObject:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _resolve_paths(value: JsonObject, base_dir: Path) -> JsonObject:
    resolved = deepcopy(value)
    qc = resolved.get("quality_control")
    if isinstance(qc, dict) and qc.get("prompt"):
        path = Path(str(qc["prompt"])).expanduser()
        qc["prompt"] = str((path if path.is_absolute() else base_dir / path).resolve())
    inputs = resolved.get("inputs")
    if isinstance(inputs, dict):
        for key in ("source_root", "taxonomy", "upstream_cluster_population"):
            if not inputs.get(key):
                continue
            path = Path(str(inputs[key])).expanduser()
            inputs[key] = str((path if path.is_absolute() else base_dir / path).resolve())
    large_inputs = resolved.get("large_inputs")
    if isinstance(large_inputs, dict) and large_inputs.get("cluster_cache_root"):
        path = Path(str(large_inputs["cluster_cache_root"])).expanduser()
        large_inputs["cluster_cache_root"] = str(
            (path if path.is_absolute() else base_dir / path).resolve()
        )
    return resolved


def load_pipeline_config(
    config_path: Path | None = None,
    overrides: JsonObject | None = None,
) -> tuple[JsonObject, Path]:
    """Merge package defaults, a JSON config, and CLI overrides."""
    def load_object(path: Path) -> JsonObject:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"配置文件必须包含 JSON 对象: {path}")
        return value

    default_path = DEFAULT_CONFIG_PATH.resolve()
    resolved_path = (config_path or default_path).expanduser().resolve()
    config = _resolve_paths(load_object(default_path), default_path.parent)
    if resolved_path != default_path:
        config = _deep_merge(
            config, _resolve_paths(load_object(resolved_path), resolved_path.parent)
        )
    if overrides:
        config = _deep_merge(config, _resolve_paths(overrides, Path.cwd()))
    _validate_config(config)
    return config, resolved_path


def _validate_scores(value: Any, location: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(type(score) is not int or not 1 <= score <= 5 for score in value)
    ):
        raise ValueError(f"{location} 必须是 1～5 的非空整数数组")


def _validate_config(config: JsonObject) -> None:
    def reject_secrets(value: Any, location: str = "config") -> None:
        if not isinstance(value, dict):
            return
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"api_key", "llm_api_key", "authorization"}:
                raise ValueError(
                    f"{location}.{key} 不得写入链路配置；请填写 llm_config.local.json"
                )
            reject_secrets(nested, f"{location}.{key}")

    reject_secrets(config)
    if "llm" in config:
        raise ValueError(
            "llm 分组已移至 artifacts/config/llm_config.local.json"
        )
    required = {
        "common",
        "inputs",
        "quality_control",
        "p0",
        "p1_coverage",
        "p2",
        "publication",
        "large_inputs",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError(f"配置缺少分组: {', '.join(missing)}")
    if any(not isinstance(config[name], dict) for name in required):
        raise ValueError("所有配置分组必须是 JSON 对象")
    if not isinstance(config["common"].get("sets"), list):
        raise ValueError("common.sets 必须是数组")
    if config["common"].get("log_level") not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("common.log_level 非法")
    resolve_selected_sets(config)

    qc = config["quality_control"]
    if "model" in qc:
        raise ValueError(
            "quality_control.model 已移至 artifacts/config/llm_config.local.json"
        )
    if not str(qc.get("prompt") or "").strip():
        raise ValueError("quality_control.prompt 不能为空")
    for key in ("workers", "max_retries"):
        if type(qc.get(key)) is not int or qc[key] < 1:
            raise ValueError(f"quality_control.{key} 必须是正整数")
    if type(qc.get("limit")) is not int or qc["limit"] < 0:
        raise ValueError("quality_control.limit 必须是非负整数")
    if not isinstance(qc.get("sleep"), (int, float)) or qc["sleep"] < 0:
        raise ValueError("quality_control.sleep 必须是非负数")

    for section in ("p0", "p1_coverage", "p2"):
        _validate_scores(config[section].get("scores"), f"{section}.scores")
        accept = config[section].get("accept")
        expected_accept = {
            "p0": ["KEEP"],
            "p1_coverage": ["KEEP"],
            "p2": ["KEEP", "TESTABLE_DIFFICULTY_REVIEW"],
        }[section]
        if accept != expected_accept:
            raise ValueError(f"{section}.accept 当前必须为 {expected_accept}")
        supplement = config[section].get("supplement")
        if not isinstance(supplement, dict):
            raise ValueError(f"{section}.supplement 必须是对象")
        if type(supplement.get("enabled")) is not bool:
            raise ValueError(f"{section}.supplement.enabled 必须是布尔值")
        strategy = supplement.get("strategy")
        allowed = {"none"} if section != "p1_coverage" else {"none", "coverage_quota"}
        if strategy not in allowed:
            raise ValueError(f"{section}.supplement.strategy 当前不支持: {strategy!r}")
        if supplement["enabled"] != (strategy != "none"):
            raise ValueError(f"{section}.supplement enabled 与 strategy 不一致")
    p1 = config["p1_coverage"]
    if not isinstance(p1.get("alpha"), (int, float)) or p1["alpha"] < 0:
        raise ValueError("p1_coverage.alpha 不能小于 0")
    if p1.get("target_n") is not None and (
        type(p1["target_n"]) is not int or p1["target_n"] < 1
    ):
        raise ValueError("p1_coverage.target_n 必须为 null 或正整数")
    for key in ("seed", "max_rounds"):
        if type(p1.get(key)) is not int or (key == "max_rounds" and p1[key] < 0):
            raise ValueError(f"p1_coverage.{key} 非法")
    rate = p1.get("default_pass_rate")
    if not isinstance(rate, (int, float)) or not 0 < rate <= 1:
        raise ValueError("p1_coverage.default_pass_rate 必须在 (0,1] 内")
    strength = p1.get("pass_rate_prior_strength")
    if not isinstance(strength, (int, float)) or strength <= 0:
        raise ValueError("p1_coverage.pass_rate_prior_strength 必须大于 0")
    min_rate = p1.get("min_pass_rate")
    max_rate = p1.get("max_pass_rate")
    if (
        not isinstance(min_rate, (int, float))
        or not isinstance(max_rate, (int, float))
        or not 0 < min_rate <= max_rate <= 1
    ):
        raise ValueError("p1_coverage 通过率上下界必须满足 0 < min <= max <= 1")
    safety = p1.get("safety_factor")
    if not isinstance(safety, (int, float)) or safety <= 0:
        raise ValueError("p1_coverage.safety_factor 必须大于 0")
    if type(config["publication"].get("enabled")) is not bool:
        raise ValueError("publication.enabled 必须是布尔值")
    large = config["large_inputs"]
    for key in ("index_batch_size", "progress_every", "sqlite_cache_mb"):
        if type(large.get(key)) is not int or large[key] < 1:
            raise ValueError(f"large_inputs.{key} 必须是正整数")
    for key in ("min_free_space_gb", "reserve_free_space_gb"):
        if not isinstance(large.get(key), (int, float)) or large[key] < 0:
            raise ValueError(f"large_inputs.{key} 必须是非负数")
    if large["min_free_space_gb"] < large["reserve_free_space_gb"]:
        raise ValueError("large_inputs.min_free_space_gb 不能小于 reserve_free_space_gb")
    for key in ("verify_full_cluster_hash", "cleanup_index_after_publish"):
        if type(large.get(key)) is not bool:
            raise ValueError(f"large_inputs.{key} 必须是布尔值")


def resolve_selected_sets(config: JsonObject) -> list[str]:
    values = config["common"]["sets"]
    selected = list(dict.fromkeys(str(value).upper() for value in values))
    unknown = [value for value in selected if value not in SET_FILES]
    if not selected or unknown:
        raise ValueError(f"配置中的数据集非法: {unknown or values}")
    return selected


def build_source_snapshot(
    source_root: Path,
    selected_sets: list[str],
    *,
    failure_audit_path: Path | None = None,
) -> JsonObject:
    resolved_root = source_root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise FileNotFoundError(f"原始数据目录不存在: {resolved_root}")
    files: JsonObject = {}
    field_audit: JsonObject = {
        "sets": {},
        "duplicate_identities": 0,
        "invalid_field_counts": {},
        "issue_count": 0,
        "issue_samples": [],
    }
    identity_observations: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    issues: list[JsonObject] = field_audit["issue_samples"]

    def record_issue(path: Path, set_name: str, line: int, code: str, **extra: Any) -> None:
        field_audit["issue_count"] = int(field_audit["issue_count"]) + 1
        counts = field_audit["invalid_field_counts"]
        counts[code] = int(counts.get(code, 0)) + 1
        if len(issues) < 20:
            issues.append(
                {
                    "file": str(path),
                    "set": set_name,
                    "source_line": line,
                    "code": code,
                    **extra,
                }
            )
    snapshot_sets = list(selected_sets)
    if "P1" in selected_sets:
        snapshot_sets.extend(
            set_name
            for set_name, relative_path in SET_FILES.items()
            if set_name not in snapshot_sets and (resolved_root / relative_path).is_file()
        )
    for set_name in snapshot_sets:
        path = resolved_root / SET_FILES[set_name]
        if not path.is_file():
            raise FileNotFoundError(f"缺少 {set_name} labeled 文件: {path}")
        records = 0
        score_counts: dict[str, int] = {}
        nonempty_query_rows = 0
        nonempty_identity_rows = 0
        cluster_id_type_counts = {"integer": 0, "string": 0, "missing": 0}
        for line, row in iter_jsonl(path):
            records += 1
            score = row.get("quality_overall_score")
            if type(score) is not int or not 1 <= score <= 5:
                record_issue(path, set_name, line, "invalid_quality_overall_score")
            else:
                score_counts[str(score)] = score_counts.get(str(score), 0) + 1
            candidate = build_candidate(row)
            target_query = str(candidate.get("target_query") or "").strip()
            if target_query:
                nonempty_query_rows += 1
            else:
                record_issue(path, set_name, line, "empty_target_query")
            identity = sample_identity(row)
            if identity[0] and identity[1]:
                nonempty_identity_rows += 1
            else:
                record_issue(path, set_name, line, "empty_dedup_identity")
            cluster_id = row.get("cluster")
            if "cluster" not in row:
                cluster_id_type_counts["missing"] += 1
            elif type(cluster_id) is int:
                cluster_id_type_counts["integer"] += 1
            elif (
                isinstance(cluster_id, str)
                and cluster_id
                and cluster_id == cluster_id.strip()
            ):
                cluster_id_type_counts["string"] += 1
            else:
                record_issue(path, set_name, line, "invalid_cluster_type")
            if identity[0] and identity[1] and target_query:
                target_hash = stable_hash(target_query)
                cluster_key = json.dumps(cluster_id, ensure_ascii=False, sort_keys=True)
                previous = identity_observations.get(identity)
                if previous is None:
                    identity_observations[identity] = (
                        target_hash, cluster_key, set_name, line
                    )
                elif previous[:2] != (target_hash, cluster_key):
                    record_issue(
                        path,
                        set_name,
                        line,
                        "dedup_identity_collision",
                        related_set=previous[2],
                        related_source_line=previous[3],
                    )
                else:
                    field_audit["duplicate_identities"] += 1
        field_audit["sets"][set_name] = {
            "file": str(path),
            "records": records,
            "quality_score_counts": dict(sorted(score_counts.items())),
            "valid_quality_score_rows": sum(score_counts.values()),
            "nonempty_query_rows": nonempty_query_rows,
            "empty_query_rows": records - nonempty_query_rows,
            "nonempty_identity_rows": nonempty_identity_rows,
            "empty_identity_rows": records - nonempty_identity_rows,
            "cluster_id_type_counts": cluster_id_type_counts,
        }
        files[set_name] = {
            "path": SET_FILES[set_name].as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "records": records,
        }
    if issues:
        audit_suffix = ""
        if failure_audit_path is not None:
            resolved_audit = failure_audit_path.expanduser().resolve()
            write_json(
                resolved_audit,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "source_root": str(resolved_root),
                    "source": field_audit,
                },
            )
            audit_suffix = f"；审计文件: {resolved_audit}"
        raise ValueError(
            f"source 输入契约存在 {field_audit['issue_count']} 个歧义问题；"
            f"样例={issues!r}；未启动任何 LLM 调用{audit_suffix}"
        )
    identity = {"source_root": str(resolved_root), "files": files}
    return {
        "name": resolved_root.name,
        "source_root": str(resolved_root),
        "source_id": stable_hash(identity, 8),
        "sets": files,
        "selected_sets": list(selected_sets),
        "field_audit": field_audit,
    }


def lightweight_preflight(
    source_root: Path,
    output_root: Path,
    config: JsonObject,
) -> JsonObject:
    """Validate configuration and sample the large input without creating a run."""
    selected_sets = resolve_selected_sets(config)
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"原始数据目录不存在: {source_root}")
    if output_root == source_root or output_root.is_relative_to(source_root):
        raise ValueError("output_root 不能等于或位于 source_root 内部")

    def writable_parent(path: Path) -> Path:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.is_dir() or not os.access(probe, os.W_OK):
            raise PermissionError(f"目标目录不可写: {path}")
        return probe

    output_parent = writable_parent(output_root)
    source_files: JsonObject = {}
    for set_name in selected_sets:
        path = source_root / SET_FILES[set_name]
        if not path.is_file() or not os.access(path, os.R_OK):
            raise FileNotFoundError(f"缺少或无法读取 {set_name} labeled 文件: {path}")
        source_files[set_name] = {"path": str(path), "size": path.stat().st_size}
    prompt = Path(config["quality_control"]["prompt"])
    load_prompt_template(prompt)
    result: JsonObject = {
        "selected_sets": selected_sets,
        "source_files": source_files,
        "prompt": str(prompt),
        "cluster_fully_validated": False,
        "output_disk_free_bytes": shutil.disk_usage(output_parent).free,
    }
    if "P1" in selected_sets:
        for key in ("taxonomy", "upstream_cluster_population"):
            if not str(config["inputs"].get(key) or "").strip():
                raise ValueError(f"处理 P1 时必须配置 inputs.{key}")
        taxonomy = Path(config["inputs"]["taxonomy"])
        cluster = Path(config["inputs"]["upstream_cluster_population"])
        load_taxonomy_mappings(taxonomy)
        if not cluster.is_file() or not os.access(cluster, os.R_OK):
            raise FileNotFoundError(f"cluster 文件不存在或不可读: {cluster}")
        samples = 0
        with cluster.open("rb") as handle:
            for physical_line in range(1, 101):
                raw = handle.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"cluster 头部第 {physical_line} 行不是合法 UTF-8 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"cluster 头部第 {physical_line} 行必须是 JSON 对象")
                samples += 1
                if samples >= 10:
                    break
        result["cluster"] = {
            "path": str(cluster.resolve()),
            "size": cluster.stat().st_size,
            "sampled_records": samples,
        }
        result["taxonomy"] = str(taxonomy.resolve())
        cache_root = _cluster_cache_root(config, output_root)
        if cache_root == source_root or cache_root.is_relative_to(source_root):
            raise ValueError("cluster_cache_root 不能等于或位于 source_root 内部")
        cache_parent = writable_parent(cache_root)
        free_bytes = shutil.disk_usage(cache_parent).free
        required = int(float(config["large_inputs"]["min_free_space_gb"]) * 1024**3)
        if free_bytes < required:
            raise OSError(
                f"cluster 索引工作区可用空间不足 {required / 1024**3:.1f} GiB: {cache_root}"
            )
        result["cluster_cache_root"] = str(cache_root)
        result["cluster_cache_free_bytes"] = free_bytes
    return result


def _cluster_cache_root(config: JsonObject, output_root: Path) -> Path:
    configured = config["large_inputs"].get("cluster_cache_root")
    return (
        Path(str(configured)).expanduser().resolve()
        if configured
        else output_root.expanduser().resolve() / "_cluster_cache"
    )


def _validate_p1_source_mapping(
    source_root: Path,
    mappings: Any,
    *,
    failure_audit_path: Path | None = None,
    source_audit: JsonObject | None = None,
) -> JsonObject:
    path = source_root / SET_FILES["P1"]
    stats: JsonObject = {
        "mapped_rows": 0,
        "unmapped_rows": 0,
        "rows_without_cluster_using_l1": 0,
    }
    issues: list[JsonObject] = []
    issue_count = 0

    def record_issue(line: int, code: str, **extra: Any) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < 20:
            issues.append(
                {
                    "file": str(path),
                    "source_line": line,
                    "code": code,
                    **extra,
                }
            )

    for line, row in iter_jsonl(path):
        raw_l1 = str(row.get("L1") or "").strip()
        if "cluster" not in row:
            if raw_l1 in mappings.l1_population:
                stats["rows_without_cluster_using_l1"] += 1
            else:
                record_issue(line, "missing_cluster_and_unknown_l1")
            continue
        cluster_id = row.get("cluster")
        if cluster_id in {-1, "-1"}:
            stats["unmapped_rows"] += 1
            continue
        cluster_type = (
            "integer" if type(cluster_id) is int
            else "string" if isinstance(cluster_id, str) else "invalid"
        )
        if cluster_type != mappings.cluster_id_type:
            record_issue(
                line,
                "cluster_id_type_mismatch",
                cluster=cluster_id,
            )
            continue
        mapped_l1 = mappings.cluster_to_l1.get(cluster_id)
        if mapped_l1 is None:
            stats["unmapped_rows"] += 1
        elif raw_l1 and raw_l1 != mapped_l1:
            record_issue(
                line,
                "l1_mapping_conflict",
                cluster=cluster_id,
            )
        else:
            stats["mapped_rows"] += 1
    if issues:
        audit_suffix = ""
        if failure_audit_path is not None:
            resolved_audit = failure_audit_path.expanduser().resolve()
            write_json(
                resolved_audit,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "source_root": str(source_root),
                    "source": source_audit or {},
                    "p1_source_mapping": {
                        **stats,
                        "issue_count": issue_count,
                        "issue_samples": issues,
                    },
                },
            )
            audit_suffix = f"；审计文件: {resolved_audit}"
        raise ValueError(
            f"P1 source 与 taxonomy 契约存在 {issue_count} 个歧义问题；"
            f"样例={issues!r}；未启动任何 LLM 调用{audit_suffix}"
        )
    return stats


def _input_metadata(
    config: JsonObject,
    selected_sets: list[str],
    output_root: Path,
    source_root: Path,
    *,
    failure_audit_path: Path | None = None,
    source_audit: JsonObject | None = None,
) -> JsonObject:
    if "P1" not in selected_sets:
        return {}
    inputs = config["inputs"]
    for key in ("taxonomy", "upstream_cluster_population"):
        if not str(inputs.get(key) or "").strip():
            raise ValueError(f"处理 P1 时必须配置 inputs.{key}")
    taxonomy_path = Path(str(inputs.get("taxonomy") or "")).expanduser().resolve()
    upstream_path = Path(
        str(inputs.get("upstream_cluster_population") or "")
    ).expanduser().resolve()
    if not taxonomy_path.is_file():
        raise FileNotFoundError(f"P1 taxonomy 不存在: {taxonomy_path}")
    if not upstream_path.is_file():
        raise FileNotFoundError(f"P1 cluster 文件不存在: {upstream_path}")
    taxonomy_hash = sha256_file(taxonomy_path)
    try:
        mappings = load_taxonomy_mappings(taxonomy_path)
    except ValueError as exc:
        audit_suffix = ""
        if failure_audit_path is not None:
            resolved_audit = failure_audit_path.expanduser().resolve()
            write_json(
                resolved_audit,
                {
                    "schema_version": 1,
                    "status": "failed",
                    "source_root": str(source_root),
                    "source": source_audit or {},
                    "taxonomy": {
                        "file": str(taxonomy_path),
                        "issue_count": 1,
                        "issue_samples": [{"code": "invalid_taxonomy_contract"}],
                    },
                },
            )
            audit_suffix = f"；审计文件: {resolved_audit}"
        raise ValueError(f"{exc}{audit_suffix}") from exc
    p1_source_mapping = _validate_p1_source_mapping(
        source_root,
        mappings,
        failure_audit_path=failure_audit_path,
        source_audit=source_audit,
    )
    large = config["large_inputs"]
    handle = ensure_cluster_index(
        upstream_path,
        mappings,
        taxonomy_sha256=taxonomy_hash,
        cache_root=_cluster_cache_root(config, output_root),
        seed=int(config["p1_coverage"]["seed"]),
        batch_size=int(large["index_batch_size"]),
        progress_every=int(large["progress_every"]),
        min_free_bytes=int(float(large["min_free_space_gb"]) * 1024**3),
        reserve_free_bytes=int(float(large["reserve_free_space_gb"]) * 1024**3),
        cache_mb=int(large["sqlite_cache_mb"]),
        require_database=False,
    )
    population = handle.metadata["population"]
    source = handle.metadata["source"]
    metadata: JsonObject = {
        "taxonomy": {
            "path": str(taxonomy_path),
            "size": taxonomy_path.stat().st_size,
            "sha256": taxonomy_hash,
        },
        "upstream_cluster_population": {
            **source,
            "sha256": handle.metadata["sha256"],
            "sample_fingerprints": handle.metadata.get("sample_fingerprints"),
            "cluster_stat_id": handle.metadata["cluster_stat_id"],
            "index_version": handle.metadata["index_version"],
            "index_status": handle.metadata["index_status"],
            "cleanup_status": handle.metadata.get("cleanup_status"),
            "cache_dir": str(handle.cache_dir),
            "metadata_path": str(handle.metadata_path),
            "metadata_size": handle.metadata_path.stat().st_size,
            "metadata_sha256": sha256_file(handle.metadata_path),
            "database_path": str(handle.database_path),
            "input_audit_path": str(handle.cache_dir / INPUT_AUDIT_NAME),
        },
    }
    metadata["validation"] = {
        "count_mode": population["count_mode"],
        "taxonomy_total": population["taxonomy_total"],
        "cluster_rows": population["total_rows"],
        "mapped_rows": population["mapped_rows"],
        "unmapped_rows": population["unmapped_rows"],
        "noise_rows": population["noise_rows"],
        "frequency_sum": population["frequency_sum"],
        "mapped_frequency_sum": population["mapped_frequency_sum"],
        "p1_source_mapping": p1_source_mapping,
    }
    return metadata


def cluster_index_handle_from_manifest(run_root: Path) -> ClusterIndexHandle:
    """Resolve the cache handle recorded by a run without scanning cluster data."""
    manifest = _read_manifest(run_root / "manifest.json")
    value = manifest.get("inputs", {}).get("upstream_cluster_population")
    if not isinstance(value, dict):
        raise ValueError("manifest 缺少 cluster 索引信息")
    metadata_path = Path(str(value.get("metadata_path", ""))).expanduser().resolve()
    metadata = _read_manifest(metadata_path)
    cache_dir = metadata_path.parent
    return ClusterIndexHandle(
        cache_dir=cache_dir,
        database_path=Path(str(value.get("database_path", cache_dir / "cluster_index.sqlite"))).expanduser().resolve(),
        metadata_path=metadata_path,
        metadata=metadata,
    )


def ensure_context_cluster_index(context: RunContext) -> ClusterIndexHandle | None:
    """Ensure an incomplete/new P1 run has a queryable index before model calls."""
    if "P1" not in resolve_selected_sets(context.config):
        return None
    inputs = context.manifest["inputs"]
    taxonomy = inputs["taxonomy"]
    cluster = inputs["upstream_cluster_population"]
    mappings = load_taxonomy_mappings(Path(taxonomy["path"]))
    large = context.config["large_inputs"]
    handle = ensure_cluster_index(
        Path(cluster["path"]),
        mappings,
        taxonomy_sha256=str(taxonomy["sha256"]),
        cache_root=_cluster_cache_root(context.config, context.output_root),
        seed=int(context.config["p1_coverage"]["seed"]),
        batch_size=int(large["index_batch_size"]),
        progress_every=int(large["progress_every"]),
        min_free_bytes=int(float(large["min_free_space_gb"]) * 1024**3),
        reserve_free_bytes=int(float(large["reserve_free_space_gb"]) * 1024**3),
        cache_mb=int(large["sqlite_cache_mb"]),
        require_database=True,
    )
    if handle.metadata.get("sha256") != cluster.get("sha256"):
        raise ValueError("重建 cluster 索引得到的 SHA-256 与 run 清单不一致")
    record_cluster_index_state(context.run_root, handle)
    return handle


def _prompt_metadata(config: JsonObject) -> JsonObject:
    path = Path(config["quality_control"]["prompt"])
    if not path.is_file():
        raise FileNotFoundError(f"缺少统一质检 Prompt: {path}")
    load_prompt_template(path)
    return {
        "quality_control": {
            "source": str(path.resolve()),
            "sha256": sha256_file(path),
        }
    }


def compute_config_id(
    config: JsonObject,
    prompts: JsonObject,
    inputs: JsonObject | None = None,
    llm_metadata: JsonObject | None = None,
) -> str:
    p1 = config["p1_coverage"]
    raw_inputs = inputs or {}
    semantic_inputs: JsonObject = {}
    for name in ("taxonomy", "upstream_cluster_population"):
        value = raw_inputs.get(name)
        if isinstance(value, dict):
            semantic_inputs[name] = {
                key: value.get(key)
                for key in ("path", "size", "sha256", "index_version")
                if key in value
            }
    semantic = {
        "pipeline_version": PIPELINE_VERSION,
        "sets": config["common"]["sets"],
        "inputs": semantic_inputs,
        "quality_control": {"limit": config["quality_control"]["limit"]},
        "p0": config["p0"],
        "p1_coverage": p1,
        "p2": config["p2"],
        "llm": llm_metadata or {},
        "prompt_hashes": {name: value["sha256"] for name, value in prompts.items()},
    }
    return stable_hash(semantic, 8)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._-")
    return normalized or "dataset"


def _artifact_map(selected_sets: list[str], config: JsonObject) -> JsonObject:
    artifacts: JsonObject = {
        "log": "logs/pipeline.log",
        "input_audit": "input_audit.json",
        "screening_summary": "screening_summary.json",
        "report": "supplement_report.md",
        "sets": {},
    }
    for name in selected_sets:
        branch: JsonObject = {
            "prescreen_attempts": f"{name}/prescreen_attempts.jsonl",
            "prescreen_results": f"{name}/prescreen_results.jsonl",
            "prescreen": f"{name}/{PRESCREEN_FILES[name].name}",
            "screened": f"{name}/{SCREENED_FILES[name].name}",
        }
        if name == "P1":
            branch.update(
                {
                    "supplement_attempts": "P1/supplement_attempts.jsonl",
                    "supplement_results": "P1/supplement_results.jsonl",
                    "supplement_tasks": "P1/supplement_tasks.jsonl",
                    "supplement_summary": "P1/supplement_summary.json",
                    "screened_provenance": "P1/screened_provenance.jsonl",
                }
            )
            if config["p1_coverage"]["supplement"]["enabled"]:
                branch["supplement"] = f"P1/{SUPPLEMENT_FILES['P1'].name}"
        artifacts["sets"][name] = branch
    return artifacts


def _run_input_audit(source: JsonObject, inputs: JsonObject) -> JsonObject:
    cluster = inputs.get("upstream_cluster_population")
    if isinstance(cluster, dict):
        cache_path = Path(str(cluster.get("input_audit_path") or ""))
        if not cache_path.is_file():
            raise FileNotFoundError(f"缺少 cluster 输入审计: {cache_path}")
        audit = _read_manifest(cache_path)
    else:
        audit = {
            "schema_version": 1,
            "taxonomy": None,
            "cluster": None,
            "mapping": None,
            "assumptions": {},
        }
    audit["source"] = source.get("field_audit", {})
    audit["assumptions"] = {
        **dict(audit.get("assumptions") or {}),
        "population_basis": "taxonomy.categories[].total_size",
        "target_n_basis": "all P1 JSONL records when target_n is null",
        "dedup_basis": "session_id/session + text/target_query/raw_query",
        "unmapped_cluster_policy": "warn_and_exclude",
        "noise_cluster_policy": "exclude_without_query_or_identity_requirements",
    }
    validation = inputs.get("validation")
    if isinstance(validation, dict):
        audit["p1_source_mapping"] = validation.get("p1_source_mapping")
    return audit


def _input_audit_summary(audit: JsonObject) -> JsonObject:
    taxonomy = audit.get("taxonomy")
    computed = taxonomy.get("computed", {}) if isinstance(taxonomy, dict) else {}
    cluster = audit.get("cluster") if isinstance(audit.get("cluster"), dict) else {}
    mapping = audit.get("mapping") if isinstance(audit.get("mapping"), dict) else {}
    source = audit.get("source") if isinstance(audit.get("source"), dict) else {}
    return {
        "actual_l1_count": computed.get("l1_count"),
        "actual_l2_count": computed.get("l2_count"),
        "taxonomy_cluster_count": computed.get("assigned_cluster_count"),
        "cluster_rows": cluster.get("total_rows"),
        "observed_cluster_count": cluster.get("observed_cluster_count"),
        "cluster_only_count": mapping.get("cluster_only_count"),
        "taxonomy_only_count": mapping.get("taxonomy_only_count"),
        "cluster_invalid_records": cluster.get("invalid_records"),
        "source_invalid_records": source.get("issue_count", 0),
        "source_duplicate_identities": source.get("duplicate_identities", 0),
    }


def _read_manifest(path: Path) -> JsonObject:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"manifest 必须包含 JSON 对象: {path}")
    return value


def record_cluster_index_state(
    run_root: Path,
    handle: ClusterIndexHandle,
) -> JsonObject:
    """Atomically mirror mutable cache/cleanup state into the run manifest."""
    with _MANIFEST_LOCK:
        manifest_path = run_root / "manifest.json"
        manifest = _read_manifest(manifest_path)
        cluster = manifest.setdefault("inputs", {}).get("upstream_cluster_population")
        if not isinstance(cluster, dict):
            raise ValueError("manifest 缺少 cluster 输入信息")
        metadata = handle.metadata
        cluster.update(
            {
                "index_status": metadata.get("index_status"),
                "cleanup_status": metadata.get("cleanup_status"),
                "metadata_size": handle.metadata_path.stat().st_size,
                "metadata_sha256": sha256_file(handle.metadata_path),
                "index_size_bytes": (
                    handle.database_path.stat().st_size
                    if handle.database_path.is_file()
                    else 0
                ),
            }
        )
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest)
        return manifest


def _new_run_id(config_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}__{config_id}"


def _snapshot_prompts(run_root: Path, prompts: JsonObject) -> JsonObject:
    relative = Path("prompts/validity_filter_prompt.md")
    destination = run_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prompts["quality_control"]["source"], destination)
    return {
        "quality_control": {
            **prompts["quality_control"],
            "snapshot": relative.as_posix(),
        }
    }


def _validate_prompt_snapshots(run_root: Path, manifest: JsonObject) -> None:
    prompts = manifest.get("prompts")
    if not isinstance(prompts, dict):
        raise ValueError("manifest 缺少 Prompt 快照信息")
    for name, metadata in prompts.items():
        if not isinstance(metadata, dict):
            raise ValueError(f"manifest Prompt 信息非法: {name}")
        snapshot = ensure_within(run_root / str(metadata.get("snapshot")), run_root, "Prompt 快照")
        if not snapshot.is_file() or sha256_file(snapshot) != metadata.get("sha256"):
            raise ValueError(f"Prompt 快照缺失或已被修改: {snapshot}")
        load_prompt_template(snapshot)


def _validate_input_audit_snapshot(run_root: Path, manifest: JsonObject) -> None:
    contract = manifest.get("input_contract")
    if not isinstance(contract, dict):
        raise ValueError("manifest 缺少输入契约审计信息")
    path = ensure_within(
        run_root / str(contract.get("audit", "")),
        run_root,
        "输入审计快照",
    )
    if not path.is_file() or sha256_file(path) != contract.get("audit_sha256"):
        raise ValueError(f"输入审计快照缺失或已被修改: {path}")


def prepare_run_context(
    source_root: Path,
    output_root: Path,
    config: JsonObject,
    *,
    run_id: str | None = None,
    new_run: bool = False,
    llm_metadata: JsonObject | None = None,
) -> RunContext:
    preparation_started_at = utc_now_ms()
    preparation_started_ns = time.monotonic_ns()
    if run_id and new_run:
        raise ValueError("--run-id 与 --new-run 不能同时使用")
    selected_sets = resolve_selected_sets(config)
    resolved_output = output_root.expanduser().resolve()
    resolved_source = source_root.expanduser().resolve()
    if resolved_output == resolved_source or resolved_output.is_relative_to(resolved_source):
        raise ValueError("output_root 不能等于或位于 source_root 内部")
    if "P1" in selected_sets:
        for key in ("taxonomy", "upstream_cluster_population"):
            if not str(config["inputs"].get(key) or "").strip():
                raise ValueError(f"处理 P1 时必须配置 inputs.{key}")
        cache_root = _cluster_cache_root(config, resolved_output)
        if cache_root == resolved_source or cache_root.is_relative_to(resolved_source):
            raise ValueError("cluster_cache_root 不能等于或位于 source_root 内部")
    failure_audit_path = (
        resolved_output
        / "_input_audits"
        / f"{_slug(resolved_source.name)}__latest_failed.json"
    )
    source = build_source_snapshot(
        source_root,
        selected_sets,
        failure_audit_path=failure_audit_path,
    )
    prompts = _prompt_metadata(config)
    cluster_prepare_started_at = utc_now_ms()
    cluster_prepare_started_ns = time.monotonic_ns()
    inputs = _input_metadata(
        config,
        selected_sets,
        output_root,
        resolved_source,
        failure_audit_path=failure_audit_path,
        source_audit=source.get("field_audit", {}),
    )
    cluster_prepare_finished_at = utc_now_ms()
    cluster_prepare_duration_ms = _elapsed_ms(cluster_prepare_started_ns)
    config_id = compute_config_id(config, prompts, inputs, llm_metadata)
    dataset_root = resolved_output / f"{_slug(str(source['name']))}__{source['source_id']}"
    runs_root = dataset_root / "runs"

    candidates: list[tuple[Path, JsonObject]] = []
    if runs_root.is_dir():
        for manifest_path in runs_root.glob("*/manifest.json"):
            manifest = _read_manifest(manifest_path)
            if (
                manifest.get("pipeline_version") == PIPELINE_VERSION
                and manifest.get("source", {}).get("source_id") == source["source_id"]
                and manifest.get("config_id") == config_id
            ):
                candidates.append((manifest_path.parent, manifest))
    candidates.sort(key=lambda item: str(item[1].get("created_at", "")), reverse=True)

    selected: tuple[Path, JsonObject] | None = None
    disposition = "created"
    if run_id:
        if not re.fullmatch(r"[0-9A-Za-z._-]+", run_id):
            raise ValueError(f"非法 run ID: {run_id}")
        matches = list(resolved_output.glob(f"*/runs/{run_id}/manifest.json"))
        if len(matches) != 1:
            raise FileNotFoundError(f"找不到唯一指定 run: {run_id}")
        manifest = _read_manifest(matches[0])
        if (
            manifest.get("pipeline_version") != PIPELINE_VERSION
            or manifest.get("source", {}).get("source_id") != source["source_id"]
            or manifest.get("config_id") != config_id
        ):
            raise ValueError(f"run {run_id} 与当前源数据、外部输入或配置不兼容")
        selected = (matches[0].parent, manifest)
        disposition = "resumed"
    elif not new_run:
        incomplete = [item for item in candidates if item[1].get("status") in INCOMPLETE_STATUSES]
        complete = [item for item in candidates if item[1].get("status") in COMPLETE_STATUSES]
        if incomplete:
            selected, disposition = incomplete[0], "resumed"
        elif complete:
            selected, disposition = complete[0], "reused"

    if selected is None:
        run_root = runs_root / _new_run_id(config_id)
        run_root.mkdir(parents=True, exist_ok=False)
        snapshots = _snapshot_prompts(run_root, prompts)
        input_audit = _run_input_audit(source, inputs)
        input_audit_path = run_root / "input_audit.json"
        write_json(input_audit_path, input_audit)
        now = utc_now()
        manifest: JsonObject = {
            "schema_version": 7,
            "pipeline_version": PIPELINE_VERSION,
            "run_id": run_root.name,
            "config_id": config_id,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "source": source,
            "inputs": inputs,
            "input_contract": {
                "audit": "input_audit.json",
                "audit_sha256": sha256_file(input_audit_path),
                "summary": _input_audit_summary(input_audit),
                "population_basis": "taxonomy.categories[].total_size",
                "target_n_basis": "all P1 JSONL records when target_n is null",
                "dedup_basis": "session_id/session + text/target_query/raw_query",
                "unmapped_cluster_policy": "warn_and_exclude",
                "noise_cluster_policy": "exclude_without_query_or_identity_requirements",
            },
            "configuration": config,
            "llm": llm_metadata or {},
            "prompts": snapshots,
            "artifacts": _artifact_map(selected_sets, config),
            "stages": {
                "cluster_prepare": {
                    "status": "pending" if "P1" in selected_sets else "disabled"
                },
                **{
                    name: {"status": "pending"}
                    for name in (
                        "quality_control", "p1_coverage", "build_screened",
                        "report", "publish",
                    )
                },
            },
            "branches": {
                name: {
                    "prescreen": {"status": "pending"},
                    "supplement": {
                        "status": (
                            "pending"
                            if name == "P1" and config["p1_coverage"]["supplement"]["enabled"]
                            else "disabled"
                        ),
                        "rounds": [],
                    },
                    "build": {"status": "pending"},
                }
                for name in selected_sets
            },
            "publication": {},
        }
        write_json(run_root / "manifest.json", manifest)
        selected = (run_root, manifest)

    selected_run_root, manifest = selected
    _validate_prompt_snapshots(selected_run_root, manifest)
    _validate_input_audit_snapshot(selected_run_root, manifest)
    if disposition != "created" and manifest.get("configuration") != config:
        manifest.setdefault("configuration_history", []).append(
            {"changed_at": utc_now(), "previous": manifest.get("configuration")}
        )
        manifest["configuration"] = config
    preparation_finished_at = utc_now_ms()
    preparation_duration_ms = _elapsed_ms(preparation_started_ns)
    _merge_run_timing(
        manifest,
        phase="initialization",
        started_at=preparation_started_at,
        finished_at=preparation_finished_at,
        duration_ms=preparation_duration_ms,
        phase_duration_ms=max(
            0,
            preparation_duration_ms
            - (cluster_prepare_duration_ms if "P1" in selected_sets else 0),
        ),
        invocation_disposition=disposition,
    )
    if "P1" in selected_sets:
        _merge_timing_entry(
            _timing_target(manifest, ("stages", "cluster_prepare", "timing")),
            started_at=cluster_prepare_started_at,
            finished_at=cluster_prepare_finished_at,
            duration_ms=cluster_prepare_duration_ms,
        )
    manifest["updated_at"] = utc_now()
    write_json(selected_run_root / "manifest.json", manifest)
    return RunContext(
        source_root=resolved_source,
        output_root=resolved_output,
        dataset_root=dataset_root,
        run_root=selected_run_root,
        run_id=str(manifest["run_id"]),
        source_id=str(source["source_id"]),
        config_id=config_id,
        config=config,
        manifest=manifest,
        disposition=disposition,
    )


def update_manifest(
    run_root: Path,
    *,
    status: str | None = None,
    stage: str | None = None,
    stage_status: str | None = None,
    stage_details: JsonObject | None = None,
    publication: JsonObject | None = None,
) -> JsonObject:
    manifest_path = run_root / "manifest.json"
    with _MANIFEST_LOCK:
        manifest = _read_manifest(manifest_path)
        if status is not None:
            manifest["status"] = status
        if stage is not None:
            stage_value = manifest.setdefault("stages", {}).setdefault(stage, {})
            if stage_status is not None:
                stage_value["status"] = stage_status
            if stage_details:
                stage_value.update(stage_details)
        if publication is not None:
            manifest["publication"] = publication
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest)
        return manifest


def update_branch_manifest(
    run_root: Path,
    set_name: str,
    stage: str,
    *,
    status: str | None = None,
    details: JsonObject | None = None,
) -> JsonObject:
    """Atomically update one isolated branch stage."""
    manifest_path = run_root / "manifest.json"
    with _MANIFEST_LOCK:
        manifest = _read_manifest(manifest_path)
        branch = manifest.setdefault("branches", {}).setdefault(set_name, {})
        value = branch.setdefault(stage, {})
        if status is not None:
            value["status"] = status
        if details:
            value.update(details)
        manifest["updated_at"] = utc_now()
        write_json(manifest_path, manifest)
        return manifest


def snapshot_prompt_path(run_root: Path, name: str = "quality_control") -> Path:
    manifest = _read_manifest(run_root / "manifest.json")
    return run_root / manifest["prompts"][name]["snapshot"]


def validate_source_unchanged(
    run_root: Path,
    *,
    verify_full_cluster_hash: bool = False,
) -> JsonObject:
    manifest = _read_manifest(run_root / "manifest.json")
    source = manifest["source"]
    current_source = build_source_snapshot(
        Path(source["source_root"]),
        list(source.get("selected_sets", source["sets"])),
        failure_audit_path=run_root / "input_audit.validation_failed.json",
    )
    if current_source["source_id"] != source["source_id"] or current_source["sets"] != source["sets"]:
        raise ValueError("原始 labeled 文件在运行期间发生变化，拒绝继续或发布")
    expected_inputs = manifest.get("inputs", {})
    taxonomy = expected_inputs.get("taxonomy")
    if isinstance(taxonomy, dict):
        path = Path(str(taxonomy.get("path", ""))).expanduser().resolve()
        if (
            not path.is_file()
            or path.stat().st_size != taxonomy.get("size")
            or sha256_file(path) != taxonomy.get("sha256")
        ):
            raise ValueError("taxonomy 在运行期间发生变化")
    cluster = expected_inputs.get("upstream_cluster_population")
    if isinstance(cluster, dict):
        validate_cluster_source(
            {
                "source": cluster,
                "sha256": cluster.get("sha256"),
                "sample_fingerprints": cluster.get("sample_fingerprints"),
            },
            verify_full_hash=verify_full_cluster_hash,
        )
    return current_source
