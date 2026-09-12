"""Small, deterministic JSON and hashing helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]


def iter_jsonl(path: Path) -> Iterator[tuple[int, JsonObject]]:
    """Yield 1-based physical line numbers and JSON objects."""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} 必须是 JSON 对象")
            yield line_number, value


def load_jsonl(path: Path) -> list[JsonObject]:
    """Load all JSON objects from a JSONL file."""
    return [record for _, record in iter_jsonl(path)]


def try_parse_json(raw_output: str) -> tuple[bool, JsonObject | None]:
    """Parse a JSON object returned directly or in a Markdown code fence."""
    if not raw_output:
        return False, None
    candidates = [raw_output]
    fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        raw_output,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        candidates.append(fenced.group(1))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return True, value
    return False, None


def write_jsonl(path: Path, records: Iterable[JsonObject]) -> None:
    """Atomically write UTF-8 JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_json(path: Path, value: JsonObject) -> None:
    """Atomically write readable UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    """Return a file SHA-256 without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any, length: int = 64) -> str:
    """Hash a JSON-serializable value deterministically."""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()[:length]
