"""Atomic, contract-bound checkpoints for resumable pipeline stages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from ..shared.json_io import JsonObject, stable_hash, write_json


CHECKPOINT_SCHEMA_VERSION = 1
_NAME = re.compile(r"[0-9A-Za-z._-]+")


class CheckpointError(ValueError):
    """A checkpoint is corrupt or belongs to another frozen run contract."""


class CheckpointStore:
    def __init__(self, run_root: Path, contract: Mapping[str, Any]) -> None:
        self.root = run_root / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)
        self.contract_sha256 = stable_hash(dict(contract))

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise ValueError(f"非法检查点名称: {name}")
        return self.root / f"{name}.json"

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def save(self, name: str, payload: JsonObject) -> JsonObject:
        envelope: JsonObject = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "name": name,
            "contract_sha256": self.contract_sha256,
            "record_count": _record_count(payload),
            "payload_sha256": stable_hash(payload),
            "payload": payload,
        }
        write_json(self._path(name), envelope)
        return envelope

    def load(self, name: str) -> JsonObject | None:
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            import json

            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CheckpointError(f"无法读取检查点: {name}") from exc
        if not isinstance(envelope, dict):
            raise CheckpointError(f"检查点不是对象: {name}")
        expected = {
            "checkpoint_schema_version",
            "name",
            "contract_sha256",
            "record_count",
            "payload_sha256",
            "payload",
        }
        if set(envelope) != expected:
            raise CheckpointError(f"检查点字段不完整: {name}")
        payload = envelope.get("payload")
        if (
            envelope.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION
            or envelope.get("name") != name
            or envelope.get("contract_sha256") != self.contract_sha256
            or not isinstance(payload, dict)
            or envelope.get("payload_sha256") != stable_hash(payload)
            or envelope.get("record_count") != _record_count(payload)
        ):
            raise CheckpointError(f"检查点契约或内容校验失败: {name}")
        return payload


def _record_count(payload: Mapping[str, Any]) -> int:
    """Count the primary record collection without prescribing payload shape."""
    for key in ("records", "candidates", "members"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, Mapping):
            return sum(len(items) for items in value.values() if isinstance(items, list))
    sets = payload.get("sets")
    if isinstance(sets, Mapping):
        return sum(len(value) for value in sets.values() if isinstance(value, list))
    for key in ("results", "admission_results", "files", "quotas"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return len(value)
    return 0
