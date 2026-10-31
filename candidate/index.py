"""Streaming, resumable SQLite index for canonical candidates."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contract import (
    CANONICAL_SCHEMA_VERSION,
    DEFAULT_FIXED_FEATURE_CONFIG,
    FEATURE_RULE_VERSION,
    MAX_SQLITE_INTEGER,
    CanonicalContractError,
    FixedFeatureConfig,
    build_canonical_candidate,
    compute_fixed_features,
    conversation_sha256,
    normalize_source_signals,
    normalize_trace_fields,
    parse_conversation,
    source_identity_sha256,
)
from ..shared.json_io import JsonObject, stable_hash, write_json
from ..shared.taxonomy import TaxonomyMappings


CANONICAL_INDEX_VERSION = 4
DATABASE_NAME = "canonical_candidate_index.sqlite"
BUILDING_DATABASE_NAME = "canonical_candidate_index.building.sqlite"
METADATA_NAME = "canonical_metadata.json"
INPUT_AUDIT_NAME = "canonical_input_audit.json"
LOCK_NAME = "canonical_build.lock"
VALIDATION_NAME = "canonical_database_validation.json"
DATABASE_VALIDATION_SCHEMA_VERSION = 1
MAX_ISSUE_SAMPLES = 20

_CANDIDATE_COLUMNS = (
    "source_identity_sha256", "conversation_sha256", "source_line", "byte_offset",
    "byte_length", "record_sha256", "cluster_json", "l1", "l2", "l2_key",
    "sample_type", "cluster_probability", "frequency", "frequency_less_count",
    "cluster_frequency_count", "probability_band", "frequency_band",
    "normalized_frequency", "length_band", "complexity", "formality",
)


@dataclass(frozen=True)
class CanonicalIndexHandle:
    cache_dir: Path
    database_path: Path
    metadata_path: Path
    metadata: JsonObject


def _source_stat(path: Path) -> JsonObject:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "file_id": f"{stat.st_dev}:{stat.st_ino}",
    }


def _same_source(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(expected.get(key) == actual.get(key) for key in ("path", "size", "mtime_ns", "file_id"))


def _sample_fingerprints(path: Path, sample_size: int = 64 * 1024) -> JsonObject:
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(sample_size)
        if size > sample_size:
            handle.seek(max(0, size - sample_size))
            tail = handle.read(sample_size)
        else:
            tail = head
    return {
        "sample_size": sample_size,
        "head_sha256": hashlib.sha256(head).hexdigest(),
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
    }


def _feature_config(config: FixedFeatureConfig | Mapping[str, Any] | None) -> FixedFeatureConfig:
    if config is None:
        return DEFAULT_FIXED_FEATURE_CONFIG
    if isinstance(config, FixedFeatureConfig):
        return config
    return FixedFeatureConfig(**dict(config))


def canonical_cache_id(
    source_path: Path,
    *,
    taxonomy_sha256: str,
    feature_config: FixedFeatureConfig | Mapping[str, Any] | None = None,
) -> str:
    rules = _feature_config(feature_config)
    return stable_hash(
        {
            "source": _source_stat(source_path),
            "taxonomy_sha256": taxonomy_sha256,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "canonical_index_version": CANONICAL_INDEX_VERSION,
            "feature_rule_version": FEATURE_RULE_VERSION,
            "feature_config": rules.as_dict(),
        },
        24,
    )


def _quick_check(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and row[0] == "ok")
    except sqlite3.DatabaseError:
        return False


def _database_fingerprint(path: Path) -> JsonObject:
    return {
        "file": _source_stat(path),
        "samples": _sample_fingerprints(path),
    }


def _fast_database_open(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            schema = connection.execute("PRAGMA schema_version").fetchone()
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates'"
            ).fetchone()
            columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(candidates)"))
        return bool(schema and table and columns == _CANDIDATE_COLUMNS)
    except sqlite3.DatabaseError:
        return False


def _ensure_database_validation(
    database_path: Path,
    validation_path: Path,
    *,
    logger: logging.Logger,
) -> None:
    current = _database_fingerprint(database_path)
    previous = _load_json(validation_path)
    if (
        previous
        and previous.get("validation_schema_version") == DATABASE_VALIDATION_SCHEMA_VERSION
        and previous.get("database") == current
        and previous.get("integrity") == "ok"
        and _fast_database_open(database_path)
    ):
        logger.info("canonical SQLite fast validation complete")
        return
    logger.info("canonical SQLite full validation start")
    started = time.monotonic()
    if not _quick_check(database_path):
        raise ValueError("canonical SQLite 索引完整性失败")
    write_json(
        validation_path,
        {
            "validation_schema_version": DATABASE_VALIDATION_SCHEMA_VERSION,
            "database": current,
            "integrity": "ok",
            "validated_at": time.time(),
        },
    )
    logger.info(
        "canonical SQLite full validation complete seconds=%.2f",
        time.monotonic() - started,
    )


def _load_json(path: Path) -> JsonObject | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_lock(path: Path, source: Mapping[str, Any]) -> None:
    payload = {"pid": os.getpid(), "created_at": time.time(), "source": dict(source)}
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            existing = _load_json(path) or {}
            raw_pid = existing.get("pid")
            pid = raw_pid if type(raw_pid) is int else -1
            if _pid_alive(pid):
                raise RuntimeError(
                    f"另一个进程正在建立 canonical 索引: PID {pid}"
                ) from exc
            path.unlink(missing_ok=True)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())


def _configure(connection: sqlite3.Connection, cache_mb: int) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute(f"PRAGMA cache_size={-max(1024, cache_mb * 1024)}")


def _initial_checkpoint(source: Mapping[str, Any]) -> JsonObject:
    return {
        "source": dict(source),
        "canonical_index_version": CANONICAL_INDEX_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "feature_rule_version": FEATURE_RULE_VERSION,
        "scan_offset": 0,
        "physical_line": 0,
        "physical_lines": 0,
        "total_rows": 0,
        "candidate_rows": 0,
        "duplicate_rows": 0,
        "noise_rows": 0,
        "isolated_rows": 0,
        "frequency_stat_rows": 0,
        "missing_cluster_probability_defaulted": 0,
        "missing_frequency_defaulted": 0,
        "issue_counts": {},
        "issue_samples": [],
        "finalized": False,
    }


def _initialize(connection: sqlite3.Connection, source: Mapping[str, Any]) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS build_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS seen (
            source_identity_sha256 TEXT PRIMARY KEY,
            target_sha256 TEXT NOT NULL,
            cluster_json TEXT NOT NULL,
            source_line INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS frequency_counts (
            cluster_json TEXT NOT NULL,
            frequency INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            PRIMARY KEY(cluster_json, frequency)
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS candidates (
            source_identity_sha256 TEXT PRIMARY KEY,
            conversation_sha256 TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            byte_length INTEGER NOT NULL,
            record_sha256 TEXT NOT NULL,
            cluster_json TEXT NOT NULL,
            l1 TEXT NOT NULL,
            l2 TEXT NOT NULL,
            l2_key TEXT NOT NULL,
            sample_type TEXT NOT NULL,
            cluster_probability REAL NOT NULL,
            frequency INTEGER NOT NULL,
            frequency_less_count INTEGER,
            cluster_frequency_count INTEGER,
            probability_band TEXT NOT NULL,
            frequency_band TEXT,
            normalized_frequency REAL,
            length_band TEXT NOT NULL,
            complexity TEXT NOT NULL,
            formality TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS candidates_l1_source
            ON candidates(l1, source_line);
        CREATE INDEX IF NOT EXISTS candidates_l2_source
            ON candidates(l2_key, source_line);
        CREATE INDEX IF NOT EXISTS candidates_cluster_source
            ON candidates(cluster_json, source_line);
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO build_state(key, value) VALUES('checkpoint', ?)",
        (json.dumps(_initial_checkpoint(source), ensure_ascii=False, sort_keys=True),),
    )
    connection.commit()


def _checkpoint(connection: sqlite3.Connection) -> JsonObject:
    row = connection.execute("SELECT value FROM build_state WHERE key='checkpoint'").fetchone()
    if row is None:
        raise ValueError("canonical 索引缺少扫描检查点")
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError("canonical 索引检查点非法")
    return value


def _save_checkpoint(connection: sqlite3.Connection, checkpoint: Mapping[str, Any]) -> None:
    connection.execute(
        "UPDATE build_state SET value=? WHERE key='checkpoint'",
        (json.dumps(dict(checkpoint), ensure_ascii=False, sort_keys=True),),
    )


def _record_issue(
    checkpoint: JsonObject,
    *,
    source_line: int,
    code: str,
    cluster_id: Any = None,
    related_source_line: int | None = None,
    isolated: bool = True,
) -> None:
    if isolated:
        checkpoint["isolated_rows"] = int(checkpoint.get("isolated_rows", 0)) + 1
    counts = checkpoint.setdefault("issue_counts", {})
    assert isinstance(counts, dict)
    for issue_code in code.split(","):
        counts[issue_code] = int(counts.get(issue_code, 0)) + 1
    samples = checkpoint.setdefault("issue_samples", [])
    if isinstance(samples, list) and len(samples) < MAX_ISSUE_SAMPLES:
        sample: JsonObject = {"source_line": source_line, "code": code}
        if type(cluster_id) is int:
            sample["cluster"] = cluster_id
        elif isinstance(cluster_id, str):
            try:
                cluster_id.encode("utf-8")
            except UnicodeEncodeError:
                pass
            else:
                sample["cluster"] = cluster_id[:200]
        if related_source_line is not None:
            sample["related_source_line"] = related_source_line
        samples.append(sample)


def _cluster_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_sha256(raw_line: bytes) -> str:
    return hashlib.sha256(raw_line).hexdigest()


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _rehash_prefix(path: Path, byte_count: int) -> hashlib._Hash:
    digest = hashlib.sha256()
    remaining = byte_count
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError("canonical 索引检查点超过当前文件末尾")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest


def _parse_cluster_id(row: Mapping[str, Any], mappings: TaxonomyMappings) -> tuple[Any, str | None]:
    if "cluster" not in row:
        return None, "missing_cluster"
    cluster_id = row.get("cluster")
    if type(cluster_id) is int:
        kind = "integer"
    elif isinstance(cluster_id, str) and cluster_id and cluster_id == cluster_id.strip():
        kind = "string"
    else:
        return cluster_id, "invalid_cluster_type"
    if cluster_id in (-1, "-1"):
        return cluster_id, None
    if kind != mappings.cluster_id_type:
        return cluster_id, "cluster_id_type_mismatch"
    return cluster_id, None


def _frequency_for_stats(row: Mapping[str, Any]) -> tuple[int | None, str | None, bool]:
    if "frequency" not in row or row.get("frequency") is None:
        return 1, None, True
    value = row.get("frequency")
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "invalid_frequency_type", False
    if value < 0:
        return None, "negative_frequency", False
    if value > MAX_SQLITE_INTEGER:
        return None, "frequency_out_of_range", False
    return value, None, False


def _audit_payload(
    checkpoint: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    mappings: TaxonomyMappings,
    taxonomy_sha256: str,
    feature_config: FixedFeatureConfig,
    status: str,
) -> JsonObject:
    return {
        "schema_version": 1,
        "status": status,
        "source": {"path": source.get("path"), "size": source.get("size")},
        "taxonomy": {
            "path": mappings.source_path,
            "sha256": taxonomy_sha256,
            "declared_l1_population": dict(mappings.l1_population),
            "declared_l2_population": dict(mappings.l2_population),
        },
        "rules": {
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "canonical_index_version": CANONICAL_INDEX_VERSION,
            "feature_rule_version": FEATURE_RULE_VERSION,
            "feature_config": feature_config.as_dict(),
        },
        "counts": {
            key: checkpoint.get(key, 0)
            for key in (
                "physical_lines",
                "total_rows",
                "candidate_rows",
                "duplicate_rows",
                "noise_rows",
                "isolated_rows",
                "frequency_stat_rows",
                "missing_cluster_probability_defaulted",
                "missing_frequency_defaulted",
            )
        },
        "issue_counts": dict(checkpoint.get("issue_counts", {})),
        "issue_samples": list(checkpoint.get("issue_samples", [])),
        "privacy": "issue samples contain only source_line, cluster, issue code, and related line",
    }


def _write_audit(
    path: Path,
    checkpoint: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
    mappings: TaxonomyMappings,
    taxonomy_sha256: str,
    feature_config: FixedFeatureConfig,
    status: str,
) -> None:
    write_json(
        path,
        _audit_payload(
            checkpoint,
            source=source,
            mappings=mappings,
            taxonomy_sha256=taxonomy_sha256,
            feature_config=feature_config,
            status=status,
        ),
    )


def _frequency_groups(connection: sqlite3.Connection) -> Iterable[tuple[str, list[tuple[int, int]]]]:
    current_cluster: str | None = None
    values: list[tuple[int, int]] = []
    for cluster_json, frequency, row_count in connection.execute(
        "SELECT cluster_json, frequency, row_count FROM frequency_counts ORDER BY cluster_json, frequency"
    ):
        if current_cluster is not None and cluster_json != current_cluster:
            yield current_cluster, values
            values = []
        current_cluster = str(cluster_json)
        values.append((int(frequency), int(row_count)))
    if current_cluster is not None:
        yield current_cluster, values


def _finalize_features(
    connection: sqlite3.Connection,
    feature_config: FixedFeatureConfig,
    logger: logging.Logger | None = None,
) -> JsonObject:
    frequency_stats: JsonObject = {}
    cluster_count = 0
    for cluster_json, counts in _frequency_groups(connection):
        cluster_count += 1
        total = sum(count for _, count in counts)
        minimum = counts[0][0]
        maximum = counts[-1][0]
        less = 0
        for frequency, count in counts:
            percentile = less / total
            if percentile >= feature_config.frequency_hot_percentile:
                band = "hot"
            elif percentile >= feature_config.frequency_warm_percentile:
                band = "warm"
            else:
                band = "cold"
            min_log = math.log1p(minimum)
            max_log = math.log1p(maximum)
            normalized = (
                1.0
                if min_log == max_log
                else (math.log1p(frequency) - min_log) / (max_log - min_log)
            )
            connection.execute(
                """
                UPDATE candidates
                SET frequency_less_count=?, cluster_frequency_count=?,
                    frequency_band=?, normalized_frequency=?
                WHERE cluster_json=? AND frequency=?
                """,
                (
                    less,
                    total,
                    band,
                    normalized,
                    cluster_json,
                    frequency,
                ),
            )
            less += count
        frequency_stats[cluster_json] = {
            "row_count": total,
            "distinct_frequency_count": len(counts),
            "min_frequency": minimum,
            "max_frequency": maximum,
        }
        if logger is not None and cluster_count % 1000 == 0:
            logger.info("canonical feature finalization clusters=%d", cluster_count)

    missing = connection.execute(
        """
        SELECT COUNT(*) FROM candidates
        WHERE frequency_less_count IS NULL OR cluster_frequency_count IS NULL
           OR frequency_band IS NULL OR normalized_frequency IS NULL
        """
    ).fetchone()
    if missing and int(missing[0]):
        raise ValueError("canonical 索引存在未补全的固定特征")
    observed_l1 = {
        row[0]: int(row[1])
        for row in connection.execute("SELECT l1, COUNT(*) FROM candidates GROUP BY l1 ORDER BY l1")
    }
    observed_l2 = {
        row[0]: int(row[1])
        for row in connection.execute("SELECT l2_key, COUNT(*) FROM candidates GROUP BY l2_key ORDER BY l2_key")
    }
    observed_clusters = {
        row[0]: int(row[1])
        for row in connection.execute(
            "SELECT cluster_json, COUNT(*) FROM candidates GROUP BY cluster_json ORDER BY cluster_json"
        )
    }
    return {
        "observed_l1_candidate_counts": observed_l1,
        "observed_l2_candidate_counts": observed_l2,
        "observed_cluster_candidate_counts": observed_clusters,
        "cluster_frequency_stats": frequency_stats,
    }


def _scan(
    source_path: Path,
    building_path: Path,
    audit_path: Path,
    mappings: TaxonomyMappings,
    *,
    source_at_start: JsonObject,
    taxonomy_sha256: str,
    feature_config: FixedFeatureConfig,
    batch_size: int,
    progress_every: int,
    reserve_free_bytes: int,
    cache_mb: int,
    logger: logging.Logger,
) -> tuple[str, JsonObject, JsonObject]:
    with closing(sqlite3.connect(building_path)) as connection:
        _configure(connection, cache_mb)
        _initialize(connection, source_at_start)
        checkpoint = _checkpoint(connection)
        checkpoint_source = checkpoint.get("source")
        if not isinstance(checkpoint_source, dict) or not _same_source(checkpoint_source, source_at_start):
            raise ValueError("canonical 索引检查点对应的源文件已变化")
        if checkpoint.get("fatal_code"):
            raise ValueError(
                f"canonical 索引检查点处于不可恢复状态: {checkpoint['fatal_code']}；审计={audit_path}"
            )
        if checkpoint.get("finalized"):
            population_row = connection.execute(
                "SELECT value FROM build_state WHERE key='population'"
            ).fetchone()
            snapshot = checkpoint.get("source_snapshot_id")
            if (
                population_row is None
                or not isinstance(snapshot, str)
                or len(snapshot) != 64
            ):
                raise ValueError("canonical 已完成检查点缺少发布信息")
            population = json.loads(population_row[0])
            if not isinstance(population, dict):
                raise ValueError("canonical 已完成检查点 population 非法")
            current_digest = _rehash_prefix(source_path, int(source_at_start["size"])).hexdigest()
            if current_digest != snapshot:
                raise ValueError("cluster 文件 SHA-256 与 canonical 已完成检查点不一致")
            check = connection.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise ValueError("canonical SQLite 索引完整性检查失败")
            connection.execute("PRAGMA journal_mode=DELETE")
            return snapshot, population, checkpoint

        offset = int(checkpoint.get("scan_offset", 0))
        physical_line = int(checkpoint.get("physical_line", 0))
        digest = _rehash_prefix(source_path, offset)
        processed_since_commit = 0
        next_progress = int(checkpoint.get("total_rows", 0)) + progress_every
        started_at = time.monotonic()

        def persist(status: str) -> None:
            _save_checkpoint(connection, checkpoint)
            connection.commit()
            _write_audit(
                audit_path,
                checkpoint,
                source=source_at_start,
                mappings=mappings,
                taxonomy_sha256=taxonomy_sha256,
                feature_config=feature_config,
                status=status,
            )

        def finish_row() -> None:
            nonlocal processed_since_commit, next_progress
            processed_since_commit += 1
            if processed_since_commit >= batch_size:
                persist("building")
                processed_since_commit = 0
                if _disk_free(building_path.parent) < reserve_free_bytes:
                    raise OSError(
                        "canonical 索引工作区剩余空间低于安全余量；已保存可恢复检查点"
                    )
            if int(checkpoint.get("total_rows", 0)) >= next_progress:
                elapsed = max(time.monotonic() - started_at, 0.001)
                logger.info(
                    "canonical index rows=%d bytes=%d/%d throughput=%.2f MiB/s",
                    checkpoint["total_rows"],
                    checkpoint["scan_offset"],
                    source_at_start["size"],
                    (int(checkpoint["scan_offset"]) - offset) / elapsed / (1024 * 1024),
                )
                next_progress += progress_every

        with source_path.open("rb") as source:
            source.seek(offset)
            while True:
                byte_offset = source.tell()
                raw_line = source.readline()
                if not raw_line:
                    break
                digest.update(raw_line)
                physical_line += 1
                checkpoint["physical_line"] = physical_line
                checkpoint["physical_lines"] = physical_line
                checkpoint["scan_offset"] = source.tell()
                byte_length = len(raw_line)
                if not raw_line.strip():
                    finish_row()
                    continue
                checkpoint["total_rows"] = int(checkpoint.get("total_rows", 0)) + 1

                try:
                    decoded = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    _record_issue(
                        checkpoint,
                        source_line=physical_line,
                        code="invalid_utf8",
                        isolated=False,
                    )
                    checkpoint["fatal_code"] = "invalid_utf8"
                    persist("fatal")
                    raise ValueError(
                        f"{source_path}:{physical_line} UTF-8 损坏，字节偏移 {byte_offset}；审计={audit_path}"
                    ) from exc
                try:
                    value = json.loads(decoded)
                except json.JSONDecodeError as exc:
                    _record_issue(
                        checkpoint,
                        source_line=physical_line,
                        code="invalid_json",
                        isolated=False,
                    )
                    checkpoint["fatal_code"] = "invalid_json"
                    persist("fatal")
                    raise ValueError(
                        f"{source_path}:{physical_line} JSON 损坏，字节偏移 {byte_offset}；审计={audit_path}"
                    ) from exc
                if not isinstance(value, dict):
                    _record_issue(checkpoint, source_line=physical_line, code="record_not_object")
                    finish_row()
                    continue
                row: JsonObject = value

                cluster_id, cluster_issue = _parse_cluster_id(row, mappings)
                if cluster_issue:
                    _record_issue(
                        checkpoint,
                        source_line=physical_line,
                        code=cluster_issue,
                        cluster_id=cluster_id,
                    )
                    finish_row()
                    continue
                if cluster_id in (-1, "-1"):
                    checkpoint["noise_rows"] = int(checkpoint.get("noise_rows", 0)) + 1
                    finish_row()
                    continue

                frequency_for_stats, frequency_issue, frequency_defaulted = _frequency_for_stats(row)
                if frequency_defaulted:
                    checkpoint["missing_frequency_defaulted"] = int(
                        checkpoint.get("missing_frequency_defaulted", 0)
                    ) + 1
                if frequency_for_stats is not None:
                    connection.execute(
                        """
                        INSERT INTO frequency_counts(cluster_json, frequency, row_count) VALUES(?, ?, 1)
                        ON CONFLICT(cluster_json, frequency) DO UPDATE SET row_count=row_count+1
                        """,
                        (_cluster_json(cluster_id), frequency_for_stats),
                    )
                    checkpoint["frequency_stat_rows"] = int(checkpoint.get("frequency_stat_rows", 0)) + 1

                issue_codes: list[str] = []
                if frequency_issue:
                    issue_codes.append(frequency_issue)
                if "cluster_prob" not in row or row.get("cluster_prob") is None:
                    checkpoint["missing_cluster_probability_defaulted"] = int(
                        checkpoint.get("missing_cluster_probability_defaulted", 0)
                    ) + 1
                try:
                    signals, _ = normalize_source_signals(row)
                except CanonicalContractError as exc:
                    if exc.code not in issue_codes:
                        issue_codes.append(exc.code)
                    signals = {}

                raw_session = row.get("session")
                if not isinstance(raw_session, str) or not raw_session.strip():
                    issue_codes.append("missing_session")
                else:
                    try:
                        raw_session.encode("utf-8")
                    except UnicodeEncodeError:
                        issue_codes.append("invalid_session_encoding")
                raw_text = row.get("text")
                if not isinstance(raw_text, str) or not raw_text.strip():
                    issue_codes.append("missing_text")
                    conversation = None
                else:
                    try:
                        conversation = parse_conversation(raw_text)
                    except CanonicalContractError as exc:
                        issue_codes.append(exc.code)
                        conversation = None

                try:
                    normalize_trace_fields(row)
                except CanonicalContractError as exc:
                    issue_codes.append(exc.code)

                l1 = mappings.cluster_to_l1.get(cluster_id)
                l2_key = mappings.cluster_to_l2.get(cluster_id)
                if not isinstance(l1, str) or not l1:
                    issue_codes.append("missing_l1_mapping")
                if (
                    not isinstance(l2_key, str)
                    or not isinstance(l1, str)
                    or not l2_key.startswith(f"{l1}/")
                    or not l2_key[len(l1) + 1 :]
                ):
                    issue_codes.append("missing_l2_mapping")

                if issue_codes:
                    _record_issue(
                        checkpoint,
                        source_line=physical_line,
                        code=",".join(dict.fromkeys(issue_codes)),
                        cluster_id=cluster_id,
                    )
                else:
                    assert isinstance(raw_session, str) and isinstance(raw_text, str)
                    assert isinstance(conversation, dict) and isinstance(l1, str) and isinstance(l2_key, str)
                    source_identity = source_identity_sha256(raw_session.strip(), raw_text)
                    conversation_hash = conversation_sha256(conversation)
                    cluster_json = _cluster_json(cluster_id)
                    target_hash = hashlib.sha256(conversation["target_query"].encode("utf-8")).hexdigest()
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO seen(
                            source_identity_sha256, target_sha256, cluster_json, source_line
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (source_identity, target_hash, cluster_json, physical_line),
                    ).rowcount
                    if not inserted:
                        checkpoint["duplicate_rows"] = int(checkpoint.get("duplicate_rows", 0)) + 1
                        existing = connection.execute(
                            "SELECT target_sha256, cluster_json, source_line FROM seen WHERE source_identity_sha256=?",
                            (source_identity,),
                        ).fetchone()
                        if existing and (existing[0] != target_hash or existing[1] != cluster_json):
                            _record_issue(
                                checkpoint,
                                source_line=physical_line,
                                code="source_identity_collision",
                                cluster_id=cluster_id,
                                related_source_line=int(existing[2]),
                                isolated=False,
                            )
                            checkpoint["fatal_code"] = "source_identity_collision"
                            persist("fatal")
                            raise ValueError(
                                f"{source_path}:{physical_line} source_identity_collision；审计={audit_path}"
                            )
                    else:
                        # Text-only features are calculated now; frequency-dependent values
                        # are overwritten in one ordered post-scan pass.
                        provisional = compute_fixed_features(
                            signals,
                            conversation,
                            {
                                "total_count": 1,
                                "less_count": 0,
                                "min_frequency": signals["frequency"],
                                "max_frequency": signals["frequency"],
                            },
                            feature_config,
                        )
                        l2 = l2_key[len(l1) + 1 :]
                        connection.execute(
                            """
                            INSERT INTO candidates(
                                source_identity_sha256, conversation_sha256,
                                source_line, byte_offset, byte_length, record_sha256,
                                cluster_json, l1, l2, l2_key, sample_type,
                                cluster_probability, frequency, probability_band,
                                length_band, complexity, formality
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_identity,
                                conversation_hash,
                                physical_line,
                                byte_offset,
                                byte_length,
                                _record_sha256(raw_line),
                                cluster_json,
                                l1,
                                l2,
                                l2_key,
                                conversation["sample_type"],
                                signals["cluster_probability"],
                                signals["frequency"],
                                provisional["probability_band"],
                                provisional["length_band"],
                                provisional["complexity"],
                                provisional["formality"],
                            ),
                        )
                        checkpoint["candidate_rows"] = int(checkpoint.get("candidate_rows", 0)) + 1
                finish_row()

        persist("finalizing")
        logger.info("canonical feature finalization start")
        finalize_started = time.monotonic()
        population = _finalize_features(connection, feature_config, logger)
        logger.info(
            "canonical feature finalization complete seconds=%.2f",
            time.monotonic() - finalize_started,
        )
        population["taxonomy_l1_population"] = dict(mappings.l1_population)
        population["taxonomy_l2_population"] = dict(mappings.l2_population)
        checkpoint["source_snapshot_id"] = digest.hexdigest()
        checkpoint["finalized"] = True
        _save_checkpoint(connection, checkpoint)
        connection.execute(
            "INSERT OR REPLACE INTO build_state(key, value) VALUES('population', ?)",
            (json.dumps(population, ensure_ascii=False, sort_keys=True),),
        )
        connection.executescript("DROP TABLE seen; DROP TABLE frequency_counts;")
        connection.commit()
        logger.info("canonical SQLite integrity check start")
        check_started = time.monotonic()
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            checkpoint["fatal_code"] = "sqlite_integrity_failure"
            _write_audit(
                audit_path,
                checkpoint,
                source=source_at_start,
                mappings=mappings,
                taxonomy_sha256=taxonomy_sha256,
                feature_config=feature_config,
                status="fatal",
            )
            raise ValueError("canonical SQLite 索引完整性检查失败")
        logger.info(
            "canonical SQLite integrity check complete seconds=%.2f",
            time.monotonic() - check_started,
        )
        connection.execute("PRAGMA journal_mode=DELETE")

    source_at_end = _source_stat(source_path)
    if not _same_source(source_at_start, source_at_end):
        checkpoint["fatal_code"] = "source_changed_during_scan"
        _write_audit(
            audit_path,
            checkpoint,
            source=source_at_start,
            mappings=mappings,
            taxonomy_sha256=taxonomy_sha256,
            feature_config=feature_config,
            status="fatal",
        )
        raise ValueError("cluster 文件在 canonical 索引扫描期间发生变化")
    _write_audit(
        audit_path,
        checkpoint,
        source=source_at_start,
        mappings=mappings,
        taxonomy_sha256=taxonomy_sha256,
        feature_config=feature_config,
        status="complete",
    )
    return digest.hexdigest(), population, checkpoint


def ensure_canonical_candidate_index(
    source_path: Path,
    mappings: TaxonomyMappings,
    *,
    taxonomy_sha256: str,
    cache_root: Path,
    feature_config: FixedFeatureConfig | Mapping[str, Any] | None = None,
    batch_size: int = 50_000,
    progress_every: int = 100_000,
    min_free_bytes: int = 10 * 1024**3,
    reserve_free_bytes: int = 8 * 1024**3,
    cache_mb: int = 1024,
    verify_full_source_hash: bool = False,
    logger: logging.Logger | None = None,
) -> CanonicalIndexHandle:
    """Build, resume, or open the canonical candidate index."""
    active_logger = logger or logging.getLogger(__name__)
    source_path = source_path.expanduser().resolve()
    cache_root = cache_root.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"cluster 文件不存在: {source_path}")
    if batch_size < 1 or progress_every < 1 or cache_mb < 1:
        raise ValueError("canonical 索引批次、进度间隔和缓存大小必须为正数")
    if min_free_bytes < 0 or reserve_free_bytes < 0:
        raise ValueError("canonical 索引磁盘余量不能为负数")
    if not isinstance(taxonomy_sha256, str) or len(taxonomy_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in taxonomy_sha256.lower()
    ):
        raise ValueError("taxonomy_sha256 必须是 SHA-256 十六进制字符串")
    rules = _feature_config(feature_config)
    source = _source_stat(source_path)
    cache_dir = cache_root / canonical_cache_id(
        source_path,
        taxonomy_sha256=taxonomy_sha256,
        feature_config=rules,
    )
    database_path = cache_dir / DATABASE_NAME
    building_path = cache_dir / BUILDING_DATABASE_NAME
    metadata_path = cache_dir / METADATA_NAME
    audit_path = cache_dir / INPUT_AUDIT_NAME
    lock_path = cache_dir / LOCK_NAME
    validation_path = cache_dir / VALIDATION_NAME

    metadata = _load_json(metadata_path)
    compatibility = {
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "canonical_index_version": CANONICAL_INDEX_VERSION,
        "feature_rule_version": FEATURE_RULE_VERSION,
        "taxonomy_sha256": taxonomy_sha256,
        "feature_config": rules.as_dict(),
    }
    if metadata and all(metadata.get(key) == value for key, value in compatibility.items()):
        metadata_source = metadata.get("source")
        if (
            metadata.get("index_status") == "complete"
            and isinstance(metadata_source, dict)
            and _same_source(metadata_source, source)
        ):
            _ensure_database_validation(
                database_path,
                validation_path,
                logger=active_logger,
            )
            validate_canonical_source(
                metadata,
                verify_full_hash=verify_full_source_hash,
            )
            return CanonicalIndexHandle(cache_dir, database_path, metadata_path, metadata)

    cache_dir.mkdir(parents=True, exist_ok=True)
    if _disk_free(cache_dir) < min_free_bytes:
        raise OSError("canonical 索引工作区剩余空间不足")
    _acquire_lock(lock_path, source)
    try:
        # A database without a valid resumable checkpoint is corruption, not a
        # cache miss: silently rebuilding could hide disk damage.
        if building_path.is_file() and not _quick_check(building_path):
            raise ValueError("canonical 构建中 SQLite 索引完整性失败")
        if database_path.is_file() and not building_path.exists():
            if not _quick_check(database_path):
                raise ValueError("canonical SQLite 索引完整性失败")
            os.replace(database_path, building_path)
        digest, population, checkpoint = _scan(
            source_path,
            building_path,
            audit_path,
            mappings,
            source_at_start=source,
            taxonomy_sha256=taxonomy_sha256,
            feature_config=rules,
            batch_size=batch_size,
            progress_every=progress_every,
            reserve_free_bytes=reserve_free_bytes,
            cache_mb=cache_mb,
            logger=active_logger,
        )
        os.replace(building_path, database_path)
        metadata = {
            **compatibility,
            "source": source,
            "source_snapshot_id": digest,
            "sha256": digest,
            "sample_fingerprints": _sample_fingerprints(source_path),
            "index_status": "complete",
            "database": DATABASE_NAME,
            "input_audit": INPUT_AUDIT_NAME,
            "input_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            "population": population,
            "counts": {
                key: checkpoint.get(key, 0)
                for key in (
                    "physical_lines",
                    "total_rows",
                    "candidate_rows",
                    "duplicate_rows",
                    "noise_rows",
                    "isolated_rows",
                    "frequency_stat_rows",
                    "missing_cluster_probability_defaulted",
                    "missing_frequency_defaulted",
                )
            },
            "completed_at": time.time(),
        }
        write_json(metadata_path, metadata)
        write_json(
            validation_path,
            {
                "validation_schema_version": DATABASE_VALIDATION_SCHEMA_VERSION,
                "database": _database_fingerprint(database_path),
                "integrity": "ok",
                "validated_at": time.time(),
            },
        )
        return CanonicalIndexHandle(cache_dir, database_path, metadata_path, metadata)
    finally:
        lock_path.unlink(missing_ok=True)


def validate_canonical_source(metadata: Mapping[str, Any], *, verify_full_hash: bool = False) -> JsonObject:
    source = metadata.get("source")
    if not isinstance(source, dict):
        raise ValueError("canonical 索引元信息缺少 source")
    path = Path(str(source.get("path", ""))).expanduser().resolve()
    if not path.is_file() or not _same_source(source, _source_stat(path)):
        raise ValueError("cluster 文件路径、大小、修改时间或文件标识已变化")
    if metadata.get("sample_fingerprints") != _sample_fingerprints(path):
        raise ValueError("cluster 文件抽样指纹已变化")
    if verify_full_hash:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != metadata.get("source_snapshot_id"):
            raise ValueError("cluster 文件 SHA-256 与 canonical 快照不一致")
    return _source_stat(path)


def validate_canonical_database(
    handle: CanonicalIndexHandle,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Validate a completed database through its integrity-bound fast sidecar."""
    _ensure_database_validation(
        handle.database_path,
        handle.cache_dir / VALIDATION_NAME,
        logger=logger or logging.getLogger(__name__),
    )


def read_canonical_indexed_record(source_path: Path, locator: Mapping[str, Any]) -> JsonObject:
    """Seek to one source record and verify the exact indexed bytes."""
    offset = locator.get("byte_offset")
    length = locator.get("byte_length")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("canonical locator byte_offset 非法")
    if isinstance(length, bool) or not isinstance(length, int) or length < 1:
        raise ValueError("canonical locator byte_length 非法")
    with source_path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(length)
    if len(raw) != length:
        raise ValueError(f"cluster 定点读取长度不足: offset={offset} length={length}")
    if hashlib.sha256(raw).hexdigest() != locator.get("record_sha256"):
        raise ValueError(f"cluster 定点记录指纹不一致: offset={offset}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cluster 定点记录无法解析: offset={offset}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"cluster 定点记录不是 JSON 对象: offset={offset}")
    return value


class CanonicalCandidateIndex:
    """Read-only, non-sampling access to feature-bearing candidate locators."""

    def __init__(self, handle: CanonicalIndexHandle):
        if not handle.database_path.is_file():
            raise FileNotFoundError(f"canonical 索引不存在: {handle.database_path}")
        validate_canonical_source(handle.metadata)
        uri = f"file:{handle.database_path.as_posix()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.handle = handle

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CanonicalCandidateIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def iter_by(
        self,
        level: str,
        category: Any,
        *,
        limit: int | None = None,
        batch_size: int = 1000,
    ) -> Iterable[JsonObject]:
        """Yield deterministic source-order locators without selecting samples."""
        column = {"l1": "l1", "l2": "l2_key", "cluster": "cluster_json"}.get(level)
        if column is None:
            raise ValueError(f"不支持的 canonical 查询层级: {level}")
        if level in {"l1", "l2"} and (
            not isinstance(category, str) or not category.strip()
        ):
            raise ValueError("canonical taxonomy 查询类别必须是非空字符串")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("canonical 查询 limit 不能小于 0")
        if batch_size < 1:
            raise ValueError("canonical 查询 batch_size 必须为正数")
        query_value = _cluster_json(category) if level == "cluster" else category
        params: tuple[Any, ...]
        limit_clause = ""
        if limit is None:
            params = (query_value,)
        else:
            limit_clause = "LIMIT ?"
            params = (query_value, limit)
        cursor = self.connection.execute(
            f"""
            SELECT source_identity_sha256, conversation_sha256,
                   source_line, byte_offset, byte_length, record_sha256,
                   cluster_json, l1, l2, l2_key, sample_type,
                   cluster_probability, frequency, frequency_less_count,
                   cluster_frequency_count, probability_band,
                   frequency_band, normalized_frequency, length_band, complexity, formality
            FROM candidates
            WHERE {column}=?
            ORDER BY source_line
            {limit_clause}
            """,
            params,
        )
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield {
                    "source_identity_sha256": row[0],
                    "conversation_sha256": row[1],
                    "source_snapshot_id": self.handle.metadata["source_snapshot_id"],
                    "source_line": int(row[2]),
                    "byte_offset": int(row[3]),
                    "byte_length": int(row[4]),
                    "record_sha256": row[5],
                    "cluster_id": json.loads(row[6]),
                    "l1": row[7],
                    "l2": row[8],
                    "l2_key": row[9],
                    "sample_type": row[10],
                    "cluster_probability": float(row[11]),
                    "frequency": int(row[12]),
                    "cluster_frequency_stats": {
                        "less_count": int(row[13]),
                        "total_count": int(row[14]),
                    },
                    "features": {
                        "probability_band": row[15],
                        "frequency_band": row[16],
                        "normalized_frequency": float(row[17]),
                        "length_band": row[18],
                        "complexity": row[19],
                        "formality": row[20],
                    },
                }

    def materialize(self, source_path: Path, locator: Mapping[str, Any], taxonomy: TaxonomyMappings) -> JsonObject:
        source_row = read_canonical_indexed_record(source_path, locator)
        return build_canonical_candidate(source_row, locator, taxonomy, locator)

    def available_counts(self, level: str) -> dict[Any, int]:
        column = {"l1": "l1", "l2": "l2_key", "cluster": "cluster_json"}.get(level)
        if column is None:
            raise ValueError(f"不支持的 canonical 查询层级: {level}")
        result: dict[Any, int] = {}
        for value, count in self.connection.execute(
            f"SELECT {column}, COUNT(*) FROM candidates GROUP BY {column} ORDER BY {column}"
        ):
            key = json.loads(value) if level == "cluster" else str(value)
            result[key] = int(count)
        return result

    def cluster_counts_for_identities(self, identities: Iterable[str]) -> dict[Any, int]:
        """Count already sampled locators by cluster without scanning the source."""
        values = sorted(set(identities))
        result: dict[Any, int] = {}
        for start in range(0, len(values), 500):
            batch = values[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.connection.execute(
                f"SELECT cluster_json, COUNT(*) FROM candidates "
                f"WHERE source_identity_sha256 IN ({placeholders}) GROUP BY cluster_json",
                batch,
            )
            for cluster_json, count in rows:
                key = json.loads(cluster_json)
                result[key] = result.get(key, 0) + int(count)
        if sum(result.values()) != len(values):
            raise ValueError("已抽样 identity 无法全部在 canonical 索引中定位")
        return result
