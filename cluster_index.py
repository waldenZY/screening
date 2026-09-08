"""Streaming, resumable SQLite index for very large cluster JSONL files."""

from __future__ import annotations

import hashlib
import ctypes
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
from typing import Any, Iterable

from .core import (
    JsonObject,
    build_cluster_candidate,
    record_fingerprint,
    sample_identity,
    stable_hash,
    write_json,
)
from .coverage import L1_ONLY, TaxonomyMappings

INDEX_VERSION = 2
DATABASE_NAME = "cluster_index.sqlite"
BUILDING_DATABASE_NAME = "cluster_index.building.sqlite"
METADATA_NAME = "metadata.json"
INPUT_AUDIT_NAME = "input_audit.json"
LOCK_NAME = "build.lock"
MAX_ISSUE_SAMPLES = 20


@dataclass(frozen=True)
class ClusterIndexHandle:
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


def cluster_stat_id(
    path: Path,
    *,
    taxonomy_sha256: str,
    seed: int,
) -> str:
    """Return the cache key for immutable source and index semantics."""
    return stable_hash(
        {
            "source": _source_stat(path),
            "taxonomy_sha256": taxonomy_sha256,
            "seed": seed,
            "index_version": INDEX_VERSION,
        },
        24,
    )


def identity_sha256(record: JsonObject) -> str:
    payload = json.dumps(
        list(sample_identity(record)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def record_sha256(record: JsonObject) -> str:
    return hashlib.sha256(record_fingerprint(record).encode("utf-8")).hexdigest()


def _rank(seed: int, l1: str, identity_hash: str) -> bytes:
    return hashlib.sha256(f"{seed}\0{l1}\0{identity_hash}".encode("utf-8")).digest()


def _cluster_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_source(expected: JsonObject, actual: JsonObject) -> bool:
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


def _load_metadata(path: Path) -> JsonObject | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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


def _process_start_token(pid: int) -> str | None:
    """Return an OS process creation token so a reused PID cannot own a stale lock."""
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return fields[21]
        except (OSError, IndexError):
            return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        try:
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            return str(creation.value) if ok else None
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


def _acquire_lock(path: Path, source: JsonObject) -> None:
    payload = {
        "pid": os.getpid(),
        "process_start": _process_start_token(os.getpid()),
        "created_at": time.time(),
        "source": source,
    }
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _load_metadata(path)
            existing_pid = int((existing or {}).get("pid", -1))
            existing_start = (existing or {}).get("process_start")
            actual_start = _process_start_token(existing_pid)
            same_process = (
                _pid_alive(existing_pid)
                and (
                    existing_start is None
                    or actual_start is None
                    or str(existing_start) == str(actual_start)
                )
            )
            if same_process:
                raise RuntimeError(f"另一个进程正在建立 cluster 索引: PID {existing_pid}")
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        return


def _quick_check(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return bool(row and row[0] == "ok")
    except sqlite3.DatabaseError:
        return False


def _configure(connection: sqlite3.Connection, cache_mb: int) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute(f"PRAGMA cache_size={-max(1024, cache_mb * 1024)}")
    connection.execute("PRAGMA foreign_keys=ON")


def _initialize(connection: sqlite3.Connection, source: JsonObject) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS build_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS seen (
            identity_sha256 TEXT PRIMARY KEY,
            target_sha256 TEXT NOT NULL,
            cluster_json TEXT NOT NULL,
            source_line INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS candidates (
            identity_sha256 TEXT PRIMARY KEY,
            source_line INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            byte_length INTEGER NOT NULL,
            cluster_json TEXT NOT NULL,
            l1 TEXT NOT NULL,
            l2 TEXT NOT NULL,
            stable_rank BLOB NOT NULL,
            record_sha256 TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS candidates_l1_rank
            ON candidates(l1, stable_rank, source_line);
        CREATE TABLE IF NOT EXISTS observed_clusters (
            cluster_json TEXT PRIMARY KEY
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS unmapped_clusters (
            cluster_json TEXT PRIMARY KEY,
            row_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS mapped_cluster_counts (
            cluster_json TEXT PRIMARY KEY,
            row_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        """
    )
    initial = {
        "source": source,
        "index_version": INDEX_VERSION,
        "scan_offset": 0,
        "physical_line": 0,
        "total_rows": 0,
        "duplicate_rows": 0,
        "mapped_rows": 0,
        "unmapped_rows": 0,
        "noise_rows": 0,
        "frequency_sum": 0.0,
        "mapped_frequency_sum": 0.0,
        "invalid_frequency_rows": 0,
        "invalid_records": 0,
        "empty_query_rows": 0,
        "empty_identity_rows": 0,
        "dangerous_duplicate_rows": 0,
        "cluster_id_type_counts": {"integer": 0, "string": 0},
        "invalid_field_counts": {},
        "issue_samples": [],
    }
    connection.execute(
        "INSERT OR IGNORE INTO build_state(key, value) VALUES('checkpoint', ?)",
        (json.dumps(initial, ensure_ascii=False, sort_keys=True),),
    )
    connection.commit()


def _checkpoint(connection: sqlite3.Connection) -> JsonObject:
    row = connection.execute(
        "SELECT value FROM build_state WHERE key='checkpoint'"
    ).fetchone()
    if row is None:
        raise ValueError("cluster 索引缺少扫描检查点")
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError("cluster 索引扫描检查点非法")
    return value


def _save_checkpoint(connection: sqlite3.Connection, value: JsonObject) -> None:
    connection.execute(
        "UPDATE build_state SET value=? WHERE key='checkpoint'",
        (json.dumps(value, ensure_ascii=False, sort_keys=True),),
    )


def _rehash_prefix(path: Path, byte_count: int) -> hashlib._Hash:
    digest = hashlib.sha256()
    remaining = byte_count
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                raise ValueError("cluster 索引检查点超过当前文件末尾")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _record_contract_issue(
    checkpoint: JsonObject,
    *,
    line: int,
    code: str,
    cluster_id: Any = None,
    related_line: int | None = None,
) -> None:
    checkpoint["invalid_records"] = int(checkpoint.get("invalid_records", 0)) + 1
    field_counts = checkpoint.setdefault("invalid_field_counts", {})
    for issue_code in code.split(","):
        field_counts[issue_code] = int(field_counts.get(issue_code, 0)) + 1
    samples = checkpoint.setdefault("issue_samples", [])
    if isinstance(samples, list) and len(samples) < MAX_ISSUE_SAMPLES:
        sample: JsonObject = {"source_line": line, "code": code}
        if cluster_id is not None and isinstance(cluster_id, (int, str)):
            sample["cluster"] = cluster_id
        if related_line is not None:
            sample["related_source_line"] = related_line
        samples.append(sample)


def _taxonomy_audit(mappings: TaxonomyMappings) -> JsonObject:
    return {
        "file": mappings.source_path,
        "meta": mappings.meta,
        "computed": {
            "l1_count": len(mappings.l1_population),
            "l2_count": len(mappings.l2_population),
            "assigned_cluster_count": len(mappings.cluster_to_l1),
            "cluster_id_type": mappings.cluster_id_type,
            "l1_total_size_sum": sum(mappings.l1_population.values()),
            "l2_size_sum": sum(mappings.l2_population.values()),
        },
        "l2_size_differences": mappings.l2_size_differences,
        "meta_semantics": "opaque_unverified",
    }


def _input_audit(
    mappings: TaxonomyMappings,
    population: JsonObject,
    source: JsonObject,
) -> JsonObject:
    unmapped = population.get("unmapped_clusters", {})
    missing = population.get("missing_taxonomy_clusters", [])
    return {
        "schema_version": 1,
        "taxonomy": _taxonomy_audit(mappings),
        "cluster": {
            "file": source.get("path"),
            **{
                key: population.get(key)
                for key in (
                    "physical_lines",
                    "total_rows",
                    "observed_cluster_count",
                    "mapped_cluster_count",
                    "unique_rows",
                    "duplicate_rows",
                    "mapped_rows",
                    "unmapped_rows",
                    "noise_rows",
                    "invalid_frequency_rows",
                    "cluster_id_type_counts",
                    "invalid_records",
                    "empty_query_rows",
                    "empty_identity_rows",
                    "dangerous_duplicate_rows",
                    "invalid_field_counts",
                    "issue_samples",
                )
            },
        },
        "mapping": {
            "cluster_only_count": len(unmapped) if isinstance(unmapped, dict) else 0,
            "cluster_only": unmapped,
            "taxonomy_only_count": len(missing) if isinstance(missing, list) else 0,
            "taxonomy_only": missing,
        },
        "assumptions": {
            "population_basis": "taxonomy.categories[].total_size",
            "target_n_basis": "all P1 JSONL records when target_n is null",
            "dedup_basis": "session_id/session + text/target_query/raw_query",
            "unmapped_cluster_policy": "warn_and_exclude",
        },
    }


def _warn_mapping_differences(
    logger: logging.Logger,
    population: JsonObject,
    audit_path: Path,
) -> None:
    unmapped = population.get("unmapped_clusters", {})
    missing = population.get("missing_taxonomy_clusters", [])
    if isinstance(unmapped, dict) and unmapped:
        top = sorted(
            unmapped.items(),
            key=lambda item: (-int(item[1]), str(item[0])),
        )[:MAX_ISSUE_SAMPLES]
        logger.warning(
            "cluster 存在 %d 个 taxonomy 未映射 ID、%d 条去重记录，已排除；top20=%s；审计=%s",
            len(unmapped),
            int(population.get("unmapped_rows", 0)),
            top,
            audit_path,
        )
    if isinstance(missing, list) and missing:
        logger.warning(
            "taxonomy 存在 %d 个 cluster 文件未观测 ID；top20=%s；审计=%s",
            len(missing),
            missing[:MAX_ISSUE_SAMPLES],
            audit_path,
        )


def _invalidate_changed_source_caches(
    cache_root: Path,
    current_source: JsonObject,
    current_cache_dir: Path,
) -> None:
    """Remove only generated stale DBs for this source and retain invalidation metadata."""
    for directory in cache_root.iterdir():
        if not directory.is_dir() or directory == current_cache_dir or directory.name == "_io_locks":
            continue
        metadata_path = directory / METADATA_NAME
        metadata = _load_metadata(metadata_path)
        stale_source: JsonObject | None = None
        stale_version: int | None = None
        if metadata and isinstance(metadata.get("source"), dict):
            stale_source = metadata["source"]
            value = metadata.get("index_version")
            stale_version = value if type(value) is int else None
        else:
            building = directory / BUILDING_DATABASE_NAME
            if building.is_file() and _quick_check(building):
                try:
                    with closing(sqlite3.connect(building)) as connection:
                        checkpoint = _checkpoint(connection)
                    value = checkpoint.get("source")
                    stale_source = value if isinstance(value, dict) else None
                    version = checkpoint.get("index_version")
                    stale_version = version if type(version) is int else None
                except (sqlite3.DatabaseError, ValueError, OSError, json.JSONDecodeError):
                    stale_source = None
        if (
            not stale_source
            or stale_source.get("path") != current_source.get("path")
            or (
                _same_source(stale_source, current_source)
                and stale_version == INDEX_VERSION
            )
        ):
            continue
        for name in (
            DATABASE_NAME,
            BUILDING_DATABASE_NAME,
            LOCK_NAME,
            DATABASE_NAME + "-wal",
            DATABASE_NAME + "-shm",
            BUILDING_DATABASE_NAME + "-wal",
            BUILDING_DATABASE_NAME + "-shm",
        ):
            (directory / name).unlink(missing_ok=True)
        invalidated = dict(metadata or {})
        invalidated["source"] = stale_source
        invalidated["index_version"] = stale_version
        invalidated["index_status"] = (
            "invalidated_index_version"
            if _same_source(stale_source, current_source)
            else "invalidated_source_changed"
        )
        invalidated["cleanup_status"] = "completed"
        invalidated["invalidated_at"] = time.time()
        write_json(metadata_path, invalidated)


def _progress(
    logger: logging.Logger,
    *,
    checkpoint: JsonObject,
    started_at: float,
    source_size: int,
    database_path: Path,
) -> None:
    elapsed = max(time.monotonic() - started_at, 0.001)
    offset = int(checkpoint["scan_offset"])
    rate = offset / elapsed
    remaining = max(source_size - offset, 0)
    eta = remaining / rate if rate else math.inf
    database_size = sum(
        candidate.stat().st_size
        for candidate in (
            database_path,
            Path(str(database_path) + "-wal"),
            Path(str(database_path) + "-shm"),
        )
        if candidate.exists()
    )
    logger.info(
        "cluster index rows=%d bytes=%d/%d throughput=%.2f MiB/s elapsed=%.1fs eta=%s "
        "index=%.2f GiB disk_free=%.2f GiB",
        checkpoint["total_rows"],
        offset,
        source_size,
        rate / (1024 * 1024),
        elapsed,
        f"{eta:.1f}s" if math.isfinite(eta) else "unknown",
        database_size / (1024**3),
        _disk_free(database_path.parent) / (1024**3),
    )


def _finalize_population(
    connection: sqlite3.Connection,
    mappings: TaxonomyMappings,
    checkpoint: JsonObject,
) -> JsonObject:
    observed = {
        row[0] for row in connection.execute("SELECT cluster_json FROM observed_clusters")
    }
    taxonomy_clusters = {_cluster_key(value) for value in mappings.cluster_to_l1}
    unmapped = {
        row[0]: int(row[1])
        for row in connection.execute(
            "SELECT cluster_json, row_count FROM unmapped_clusters ORDER BY cluster_json"
        )
    }
    missing = sorted(taxonomy_clusters - observed)

    taxonomy_total = sum(mappings.l1_population.values())
    total_rows = int(checkpoint["total_rows"])
    duplicate_rows = int(checkpoint["duplicate_rows"])
    frequency_sum = float(checkpoint["frequency_sum"])
    pct_by_l1 = {
        name: count / taxonomy_total if taxonomy_total else 0.0
        for name, count in mappings.l1_population.items()
    }
    pct_differences = {
        name: {
            "declared": mappings.declared_pct[name],
            "calculated": pct_by_l1[name],
            "delta": mappings.declared_pct[name] - pct_by_l1[name],
        }
        for name in mappings.declared_pct
        if abs(mappings.declared_pct[name] - pct_by_l1[name]) > 1e-6
    }
    cluster_counts = {
        row[0]: int(row[1])
        for row in connection.execute(
            "SELECT cluster_json, row_count FROM mapped_cluster_counts ORDER BY cluster_json"
        )
    }
    return {
        "count_mode": "taxonomy_total_size",
        "deduplicate_candidates": True,
        "taxonomy_meta": mappings.meta,
        "taxonomy_total": taxonomy_total,
        "total_rows": total_rows,
        "physical_lines": int(checkpoint["physical_line"]),
        "observed_cluster_count": len(observed),
        "mapped_cluster_count": len(cluster_counts),
        "unique_rows": total_rows - duplicate_rows,
        "row_count_vs_taxonomy_delta": total_rows - taxonomy_total,
        "duplicate_rows": duplicate_rows,
        "mapped_rows": int(checkpoint["mapped_rows"]),
        "unmapped_rows": int(checkpoint["unmapped_rows"]),
        "noise_rows": int(checkpoint["noise_rows"]),
        "unmapped_clusters": unmapped,
        "missing_taxonomy_clusters": missing,
        "frequency_sum": frequency_sum,
        "mapped_frequency_sum": float(checkpoint["mapped_frequency_sum"]),
        "frequency_vs_row_count_delta": frequency_sum - total_rows,
        "frequency_vs_taxonomy_delta": frequency_sum - taxonomy_total,
        "invalid_frequency_rows": int(checkpoint["invalid_frequency_rows"]),
        "cluster_prob_used": False,
        "frequency_used": False,
        "l1_counts": dict(mappings.l1_population),
        "l1_pct": pct_by_l1,
        "pct_differences": pct_differences,
        "l2_counts": dict(mappings.l2_population),
        "cluster_counts": cluster_counts,
        "taxonomy_cluster_id_type": mappings.cluster_id_type,
        "l2_size_differences": mappings.l2_size_differences,
        "cluster_id_type_counts": dict(checkpoint.get("cluster_id_type_counts", {})),
        "invalid_records": int(checkpoint.get("invalid_records", 0)),
        "empty_query_rows": int(checkpoint.get("empty_query_rows", 0)),
        "empty_identity_rows": int(checkpoint.get("empty_identity_rows", 0)),
        "dangerous_duplicate_rows": int(
            checkpoint.get("dangerous_duplicate_rows", 0)
        ),
        "issue_samples": list(checkpoint.get("issue_samples", [])),
        "invalid_field_counts": dict(checkpoint.get("invalid_field_counts", {})),
    }


def _scan(
    source_path: Path,
    building_path: Path,
    mappings: TaxonomyMappings,
    *,
    seed: int,
    batch_size: int,
    progress_every: int,
    reserve_free_bytes: int,
    cache_mb: int,
    logger: logging.Logger,
) -> tuple[str, JsonObject]:
    source_at_start = _source_stat(source_path)
    with closing(sqlite3.connect(building_path)) as connection:
        with connection:
            _configure(connection, cache_mb)
            _initialize(connection, source_at_start)
            checkpoint = _checkpoint(connection)
        if checkpoint.get("index_version") != INDEX_VERSION:
            raise ValueError("未完成 cluster 索引版本不兼容")
        if not _same_source(checkpoint.get("source", {}), source_at_start):
            raise ValueError("cluster 文件属性已变化，不能恢复旧检查点")
        if checkpoint.get("finalized") is True:
            if int(checkpoint["scan_offset"]) != int(source_at_start["size"]):
                raise ValueError("已完成的临时 cluster 索引检查点不在文件末尾")
            row = connection.execute(
                "SELECT value FROM build_state WHERE key='population'"
            ).fetchone()
            if row is None:
                raise ValueError("已完成的临时 cluster 索引缺少 Population 快照")
            population = json.loads(row[0])
            digest = _rehash_prefix(source_path, int(checkpoint["scan_offset"]))
            if not _same_source(source_at_start, _source_stat(source_path)):
                raise ValueError("cluster 文件在索引恢复期间发生变化")
            return digest.hexdigest(), population
        offset = int(checkpoint["scan_offset"])
        physical_line = int(checkpoint["physical_line"])
        started_at = time.monotonic()
        digest = _rehash_prefix(source_path, offset)
        processed_since_commit = 0
        next_progress = ((int(checkpoint["total_rows"]) // progress_every) + 1) * progress_every

        with source_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                byte_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                digest.update(raw_line)
                physical_line += 1
                byte_length = len(raw_line)
                checkpoint["physical_line"] = physical_line
                checkpoint["scan_offset"] = byte_offset + byte_length
                if not raw_line.strip():
                    processed_since_commit += 1
                else:
                    try:
                        decoded_line = raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError(
                            f"{source_path}:{physical_line} UTF-8 非法，字节偏移 {byte_offset}"
                        ) from exc
                    try:
                        row = json.loads(decoded_line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"{source_path}:{physical_line} JSON 非法，字节偏移 {byte_offset}"
                        ) from exc
                    if not isinstance(row, dict):
                        raise ValueError(
                            f"{source_path}:{physical_line} 必须是 JSON 对象，字节偏移 {byte_offset}"
                        )
                    checkpoint["total_rows"] += 1
                    frequency = row.get("frequency")
                    valid_frequency = (
                        isinstance(frequency, (int, float))
                        and not isinstance(frequency, bool)
                        and frequency >= 0
                    )
                    if frequency is not None:
                        if valid_frequency:
                            checkpoint["frequency_sum"] += float(frequency)
                        else:
                            checkpoint["invalid_frequency_rows"] += 1

                    cluster_id = row.get("cluster")
                    issue_codes: list[str] = []
                    if "cluster" not in row:
                        issue_codes.append("missing_cluster")
                    elif type(cluster_id) is int:
                        cluster_type = "integer"
                    elif isinstance(cluster_id, str) and cluster_id and cluster_id == cluster_id.strip():
                        cluster_type = "string"
                    else:
                        issue_codes.append("invalid_cluster_type")
                        cluster_type = "invalid"
                    is_noise = cluster_id == -1 or cluster_id == "-1"
                    cluster_key = _cluster_key(cluster_id)
                    if not issue_codes:
                        type_counts = checkpoint.setdefault(
                            "cluster_id_type_counts", {"integer": 0, "string": 0}
                        )
                        type_counts[cluster_type] = int(type_counts.get(cluster_type, 0)) + 1
                    if not issue_codes and not is_noise:
                        connection.execute(
                            "INSERT OR IGNORE INTO observed_clusters(cluster_json) VALUES(?)",
                            (cluster_key,),
                        )
                        if cluster_type != mappings.cluster_id_type:
                            issue_codes.append("cluster_id_type_mismatch")

                    candidate = build_cluster_candidate(row)
                    target_query = str(candidate.get("target_query") or "").strip()
                    if not target_query:
                        checkpoint["empty_query_rows"] = int(
                            checkpoint.get("empty_query_rows", 0)
                        ) + 1
                        issue_codes.append("empty_target_query")
                    identity = sample_identity(row)
                    if not identity[0] or not identity[1]:
                        checkpoint["empty_identity_rows"] = int(
                            checkpoint.get("empty_identity_rows", 0)
                        ) + 1
                        issue_codes.append("empty_dedup_identity")
                    if issue_codes:
                        _record_contract_issue(
                            checkpoint,
                            line=physical_line,
                            code=",".join(issue_codes),
                            cluster_id=cluster_id,
                        )
                        processed_since_commit += 1
                        if processed_since_commit >= batch_size:
                            _save_checkpoint(connection, checkpoint)
                            connection.commit()
                            processed_since_commit = 0
                            if _disk_free(building_path.parent) < reserve_free_bytes:
                                raise OSError(
                                    "cluster 索引工作区剩余空间低于安全余量；已保存可恢复检查点"
                                )
                            if int(checkpoint["total_rows"]) >= next_progress:
                                _progress(
                                    logger,
                                    checkpoint=checkpoint,
                                    started_at=started_at,
                                    source_size=int(source_at_start["size"]),
                                    database_path=building_path,
                                )
                                next_progress += progress_every
                        continue

                    identity_hash = identity_sha256(row)
                    target_hash = hashlib.sha256(target_query.encode("utf-8")).hexdigest()
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO seen(
                            identity_sha256, target_sha256, cluster_json, source_line
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (identity_hash, target_hash, cluster_key, physical_line),
                    ).rowcount
                    if not inserted:
                        checkpoint["duplicate_rows"] += 1
                        existing = connection.execute(
                            """
                            SELECT target_sha256, cluster_json, source_line
                            FROM seen WHERE identity_sha256=?
                            """,
                            (identity_hash,),
                        ).fetchone()
                        if existing and (
                            existing[0] != target_hash or existing[1] != cluster_key
                        ):
                            checkpoint["dangerous_duplicate_rows"] = int(
                                checkpoint.get("dangerous_duplicate_rows", 0)
                            ) + 1
                            _record_contract_issue(
                                checkpoint,
                                line=physical_line,
                                code="dedup_identity_collision",
                                cluster_id=cluster_id,
                                related_line=int(existing[2]),
                            )
                    elif is_noise:
                        checkpoint["noise_rows"] += 1
                    else:
                        l1 = mappings.cluster_to_l1.get(cluster_id)
                        if l1 is None:
                            checkpoint["unmapped_rows"] += 1
                            connection.execute(
                                """
                                INSERT INTO unmapped_clusters(cluster_json, row_count) VALUES(?, 1)
                                ON CONFLICT(cluster_json) DO UPDATE SET row_count=row_count+1
                                """,
                                (cluster_key,),
                            )
                        else:
                            checkpoint["mapped_rows"] += 1
                            if valid_frequency:
                                checkpoint["mapped_frequency_sum"] += float(frequency)
                            connection.execute(
                                """
                                INSERT INTO mapped_cluster_counts(cluster_json, row_count) VALUES(?, 1)
                                ON CONFLICT(cluster_json) DO UPDATE SET row_count=row_count+1
                                """,
                                (cluster_key,),
                            )
                            connection.execute(
                                """
                                INSERT INTO candidates(
                                    identity_sha256, source_line, byte_offset, byte_length,
                                    cluster_json, l1, l2, stable_rank, record_sha256
                                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    identity_hash,
                                    physical_line,
                                    byte_offset,
                                    byte_length,
                                    cluster_key,
                                    l1,
                                    mappings.cluster_to_l2.get(cluster_id, L1_ONLY),
                                    _rank(seed, l1, identity_hash),
                                    record_sha256(row),
                                ),
                            )
                    processed_since_commit += 1

                if processed_since_commit >= batch_size:
                    _save_checkpoint(connection, checkpoint)
                    connection.commit()
                    processed_since_commit = 0
                    if _disk_free(building_path.parent) < reserve_free_bytes:
                        raise OSError("cluster 索引工作区剩余空间低于安全余量；已保存可恢复检查点")
                    if int(checkpoint["total_rows"]) >= next_progress:
                        _progress(
                            logger,
                            checkpoint=checkpoint,
                            started_at=started_at,
                            source_size=int(source_at_start["size"]),
                            database_path=building_path,
                        )
                        next_progress += progress_every

        _save_checkpoint(connection, checkpoint)
        connection.commit()
        population = _finalize_population(connection, mappings, checkpoint)
        checkpoint["finalized"] = True
        _save_checkpoint(connection, checkpoint)
        connection.execute(
            "INSERT OR REPLACE INTO build_state(key, value) VALUES('population', ?)",
            (json.dumps(population, ensure_ascii=False, sort_keys=True),),
        )
        connection.executescript(
            """
            DROP TABLE seen;
            DROP TABLE observed_clusters;
            DROP TABLE unmapped_clusters;
            DROP TABLE mapped_cluster_counts;
            """
        )
        connection.commit()
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("cluster SQLite 索引完整性检查失败")

    source_at_end = _source_stat(source_path)
    if not _same_source(source_at_start, source_at_end):
        raise ValueError("cluster 文件在索引扫描期间发生变化")
    return digest.hexdigest(), population


def ensure_cluster_index(
    source_path: Path,
    mappings: TaxonomyMappings,
    *,
    taxonomy_sha256: str,
    cache_root: Path,
    seed: int = 42,
    batch_size: int = 10_000,
    progress_every: int = 100_000,
    min_free_bytes: int = 3 * 1024**3,
    reserve_free_bytes: int = 1024**3,
    cache_mb: int = 64,
    require_database: bool = True,
    logger: logging.Logger | None = None,
) -> ClusterIndexHandle:
    """Return a valid cache, building or resuming it with one streaming scan."""
    active_logger = logger or logging.getLogger(__name__)
    source_path = source_path.expanduser().resolve()
    cache_root = cache_root.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"cluster 文件不存在: {source_path}")
    if batch_size < 1 or progress_every < 1 or cache_mb < 1:
        raise ValueError("cluster 索引批次、进度间隔和缓存大小必须为正数")
    source = _source_stat(source_path)
    stat_id = cluster_stat_id(source_path, taxonomy_sha256=taxonomy_sha256, seed=seed)
    cache_dir = cache_root / stat_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    database_path = cache_dir / DATABASE_NAME
    building_path = cache_dir / BUILDING_DATABASE_NAME
    metadata_path = cache_dir / METADATA_NAME
    audit_path = cache_dir / INPUT_AUDIT_NAME
    lock_path = cache_dir / LOCK_NAME
    io_lock_dir = cache_root / "_io_locks"
    io_lock_dir.mkdir(parents=True, exist_ok=True)
    io_lock_path = io_lock_dir / f"{stable_hash({'path': source['path']}, 24)}.lock"
    metadata = _load_metadata(metadata_path)
    compatible = bool(
        metadata
        and metadata.get("index_version") == INDEX_VERSION
        and metadata.get("taxonomy_sha256") == taxonomy_sha256
        and metadata.get("seed") == seed
        and _same_source(metadata.get("source", {}), source)
        and isinstance(metadata.get("sha256"), str)
        and audit_path.is_file()
        and hashlib.sha256(audit_path.read_bytes()).hexdigest()
        == metadata.get("input_audit_sha256")
    )
    if compatible and metadata is not None:
        samples_match = metadata.get("sample_fingerprints") == _sample_fingerprints(source_path)
        if samples_match and database_path.is_file() and _quick_check(database_path):
            metadata["index_status"] = "complete"
            _warn_mapping_differences(active_logger, metadata["population"], audit_path)
            return ClusterIndexHandle(cache_dir, database_path, metadata_path, metadata)
        if samples_match and not require_database and metadata.get("index_status") == "deleted":
            _warn_mapping_differences(active_logger, metadata["population"], audit_path)
            return ClusterIndexHandle(cache_dir, database_path, metadata_path, metadata)

    _acquire_lock(io_lock_path, source)
    try:
        _acquire_lock(lock_path, source)
    except Exception:
        io_lock_path.unlink(missing_ok=True)
        raise
    try:
        try:
            _invalidate_changed_source_caches(cache_root, source, cache_dir)
        except OSError as exc:
            active_logger.warning("旧 cluster 索引失效清理未完成: %s", exc)
        if _disk_free(cache_dir) < min_free_bytes:
            raise OSError(
                f"cluster 索引工作区可用空间不足 {min_free_bytes / 1024**3:.1f} GiB: {cache_dir}"
            )
        if database_path.exists() and not building_path.exists() and _quick_check(database_path):
            try:
                with closing(sqlite3.connect(database_path)) as connection:
                    orphan_checkpoint = _checkpoint(connection)
                if (
                    orphan_checkpoint.get("finalized") is True
                    and _same_source(orphan_checkpoint.get("source", {}), source)
                ):
                    os.replace(database_path, building_path)
            except (sqlite3.DatabaseError, ValueError, OSError, json.JSONDecodeError):
                pass
        if building_path.exists() and not _quick_check(building_path):
            building_path.unlink()
        if building_path.exists():
            try:
                with closing(sqlite3.connect(building_path)) as connection:
                    checkpoint = _checkpoint(connection)
                if not _same_source(checkpoint.get("source", {}), source):
                    building_path.unlink()
            except (sqlite3.DatabaseError, ValueError, OSError, json.JSONDecodeError):
                building_path.unlink(missing_ok=True)
        active_logger.info("开始或恢复 cluster 流式索引: %s", source_path)
        digest, population = _scan(
            source_path,
            building_path,
            mappings,
            seed=seed,
            batch_size=batch_size,
            progress_every=progress_every,
            reserve_free_bytes=reserve_free_bytes,
            cache_mb=cache_mb,
            logger=active_logger,
        )
        audit = _input_audit(mappings, population, source)
        write_json(audit_path, audit)
        invalid_records = int(population.get("invalid_records", 0))
        if invalid_records:
            issue_samples = population.get("issue_samples", [])
            raise ValueError(
                f"cluster 输入契约存在 {invalid_records} 个歧义问题（文件 {source_path}）；"
                f"样例={issue_samples!r}；审计文件: {audit_path}"
            )
        _warn_mapping_differences(active_logger, population, audit_path)
        database_path.unlink(missing_ok=True)
        os.replace(building_path, database_path)
        metadata = {
            "schema_version": 1,
            "index_version": INDEX_VERSION,
            "cluster_stat_id": stat_id,
            "source": source,
            "sha256": digest,
            "sample_fingerprints": _sample_fingerprints(source_path),
            "taxonomy_sha256": taxonomy_sha256,
            "seed": seed,
            "index_status": "complete",
            "cleanup_status": "pending",
            "database": DATABASE_NAME,
            "input_audit": INPUT_AUDIT_NAME,
            "input_audit_sha256": hashlib.sha256(
                audit_path.read_bytes()
            ).hexdigest(),
            "index_size_bytes": database_path.stat().st_size,
            "population": population,
            "completed_at": time.time(),
        }
        write_json(metadata_path, metadata)
        active_logger.info(
            "cluster 索引完成: rows=%d candidates=%d sha256=%s",
            population["total_rows"],
            population["mapped_rows"],
            digest,
        )
        return ClusterIndexHandle(cache_dir, database_path, metadata_path, metadata)
    finally:
        lock_path.unlink(missing_ok=True)
        io_lock_path.unlink(missing_ok=True)


def validate_cluster_source(
    metadata: JsonObject,
    *,
    verify_full_hash: bool = False,
) -> JsonObject:
    """Validate immutable-file attributes, optionally recomputing the full hash."""
    source = metadata.get("source")
    if not isinstance(source, dict):
        raise ValueError("cluster 索引元信息缺少 source")
    path = Path(str(source.get("path", ""))).expanduser().resolve()
    if not path.is_file() or not _same_source(source, _source_stat(path)):
        raise ValueError("cluster 文件路径、大小、修改时间或文件标识已变化")
    expected_samples = metadata.get("sample_fingerprints")
    if expected_samples and expected_samples != _sample_fingerprints(path):
        raise ValueError("cluster 文件抽样指纹已变化")
    if verify_full_hash:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != metadata.get("sha256"):
            raise ValueError("cluster 文件 SHA-256 与首次准备结果不一致")
    return _source_stat(path)


def read_indexed_record(source_path: Path, locator: JsonObject) -> JsonObject:
    """Seek to one indexed line and verify its immutable fingerprints."""
    offset = int(locator["byte_offset"])
    length = int(locator["byte_length"])
    with source_path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(length)
    if len(raw) != length:
        raise ValueError(f"cluster 定点读取长度不足: offset={offset} length={length}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cluster 定点记录无法解析: offset={offset}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"cluster 定点记录不是 JSON 对象: offset={offset}")
    if locator.get("record_sha256") and record_sha256(value) != locator["record_sha256"]:
        raise ValueError(f"cluster 定点记录指纹不一致: offset={offset}")
    if locator.get("identity_sha256") and identity_sha256(value) != locator["identity_sha256"]:
        raise ValueError(f"cluster 定点记录身份不一致: offset={offset}")
    return value


class CandidateIndex:
    """Read-only candidate queries with a connection-local exclusion ledger."""

    def __init__(self, handle: ClusterIndexHandle):
        if not handle.database_path.is_file():
            raise FileNotFoundError(f"cluster 索引已清理或不存在: {handle.database_path}")
        uri = f"file:{handle.database_path.as_posix()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.execute(
            "CREATE TEMP TABLE excluded(identity_sha256 TEXT PRIMARY KEY) WITHOUT ROWID"
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "CandidateIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def exclude(self, identities: Iterable[str]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO excluded(identity_sha256) VALUES(?)",
            ((identity,) for identity in identities),
        )

    def take(self, l1: str, count: int) -> list[JsonObject]:
        return self.take_by("l1", l1, count)

    def take_by(self, level: str, category: str, count: int) -> list[JsonObject]:
        if count <= 0:
            return []
        values = list(self.iter_by(level, category, limit=count))
        self.exclude(value["identity_sha256"] for value in values)
        return values

    def iter_by(
        self,
        level: str,
        category: str,
        *,
        limit: int | None = None,
        batch_size: int = 1000,
    ) -> Iterable[JsonObject]:
        """Yield locators in bounded batches without materializing a category."""
        column = {"l1": "l1", "l2": "l2"}.get(level)
        if column is None:
            raise ValueError(f"不支持的 taxonomy 层级: {level}")
        if limit is not None and limit < 0:
            raise ValueError("候选查询 limit 不能小于 0")
        if batch_size < 1:
            raise ValueError("候选查询 batch_size 必须为正数")
        limit_clause = "LIMIT ?" if limit is not None else ""
        params: tuple[object, ...] = (category, limit) if limit is not None else (category,)
        cursor = self.connection.execute(
            f"""
            SELECT c.identity_sha256, c.source_line, c.byte_offset, c.byte_length,
                   c.cluster_json, c.l1, c.l2, c.record_sha256
            FROM candidates AS c
            LEFT JOIN excluded AS e ON e.identity_sha256=c.identity_sha256
            WHERE c.{column}=? AND e.identity_sha256 IS NULL
            ORDER BY c.stable_rank, c.source_line
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
                    "identity_sha256": row[0],
                    "source_line": int(row[1]),
                    "byte_offset": int(row[2]),
                    "byte_length": int(row[3]),
                    "cluster": json.loads(row[4]),
                    "L1": row[5],
                    "L2": row[6],
                    "record_sha256": row[7],
                }

    def available_by_l1(self) -> dict[str, int]:
        return {
            row[0]: int(row[1])
            for row in self.connection.execute(
                """
                SELECT c.l1, COUNT(*)
                FROM candidates AS c
                LEFT JOIN excluded AS e ON e.identity_sha256=c.identity_sha256
                WHERE e.identity_sha256 IS NULL
                GROUP BY c.l1
                """
            )
        }

    def available_count(self, level: str, category: str) -> int:
        column = {"l1": "l1", "l2": "l2"}.get(level)
        if column is None:
            raise ValueError(f"不支持的 taxonomy 层级: {level}")
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) FROM candidates AS c
            LEFT JOIN excluded AS e ON e.identity_sha256=c.identity_sha256
            WHERE c.{column}=? AND e.identity_sha256 IS NULL
            """,
            (category,),
        ).fetchone()
        return int(row[0]) if row else 0


def cleanup_cluster_index(handle: ClusterIndexHandle) -> JsonObject:
    """Delete the large database after publication while retaining metadata."""
    metadata = dict(handle.metadata)
    try:
        handle.database_path.unlink(missing_ok=True)
        (handle.cache_dir / BUILDING_DATABASE_NAME).unlink(missing_ok=True)
        (handle.cache_dir / LOCK_NAME).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(handle.database_path) + suffix).unlink(missing_ok=True)
        metadata["index_status"] = "deleted"
        metadata["cleanup_status"] = "completed"
        metadata["cleaned_at"] = time.time()
    except OSError as exc:
        metadata["cleanup_status"] = "cleanup_pending"
        metadata["cleanup_error"] = str(exc)
    write_json(handle.metadata_path, metadata)
    return metadata
