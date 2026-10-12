"""Frozen corpus statistics and shared text rules for candidate deduplication."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contract import parse_conversation
from .index import CanonicalIndexHandle
from ..shared.json_io import stable_hash, write_json


DEDUP_RULE_VERSION = 1
NORMALIZATION_RULE = "unicode_whitespace_and_category_P_removed_v1"
IDF_RULE = "all_indexed_rows_unique_character_2_3grams_log((N+1)/(df+1))+1_v1"
RULES_SHA256 = stable_hash({"version": DEDUP_RULE_VERSION, "normalization": NORMALIZATION_RULE, "idf": IDF_RULE})


def normalize_text(value: str) -> str:
    """Remove whitespace and punctuation, preserving all content characters."""
    return "".join(char for char in value if not char.isspace() and not unicodedata.category(char).startswith("P"))


def selection_text(item: Mapping[str, Any]) -> str:
    if "conversation" in item:
        conversation = item["conversation"]
        return str(conversation["full_text"] if conversation["sample_type"] == "multi_turn" else conversation["target_query"])
    return str(item["selection_text"])


def character_grams(value: str) -> frozenset[str]:
    return frozenset(value[pos:pos + width] for width in (2, 3) for pos in range(max(0, len(value) - width + 1)))


@dataclass(frozen=True)
class DedupConfig:
    normalization_rule: str = NORMALIZATION_RULE
    idf_rule: str = IDF_RULE
    rules_sha256: str = RULES_SHA256
    candidate_jaccard_min: float = 0.45
    weighted_jaccard_min: float = 0.80
    edit_similarity_min: float = 0.90

    def __post_init__(self) -> None:
        if (self.normalization_rule != NORMALIZATION_RULE or self.idf_rule != IDF_RULE
                or self.rules_sha256 != RULES_SHA256):
            raise ValueError("Dedup text and IDF rules must match the current version")
        for name in ("candidate_jaccard_min", "weighted_jaccard_min", "edit_similarity_min"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError(f"dedup.{name} must be in [0, 1]")


def build_idf_snapshot(index: CanonicalIndexHandle, source_path: Path) -> dict[str, Any]:
    """Read every indexed source row once; order does not affect document frequency."""
    counts: Counter[str] = Counter()
    total = 0
    uri = f"file:{index.database_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database, source_path.open("rb") as source:
        for offset, length, expected_hash in database.execute(
            "SELECT byte_offset, byte_length, record_sha256 FROM candidates ORDER BY source_line"
        ):
            source.seek(offset)
            raw = source.read(length)
            if len(raw) != length or hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError(f"IDF source row changed at byte {offset}")
            row = json.loads(raw.decode("utf-8"))
            conversation = parse_conversation(row["text"])
            counts.update(character_grams(normalize_text(selection_text({"conversation": conversation}))))
            total += 1
    snapshot = {
        "rules_version": DEDUP_RULE_VERSION,
        "rules_sha256": RULES_SHA256,
        "source_snapshot_id": index.metadata["source_snapshot_id"],
        "document_count": total,
        "document_frequency": dict(sorted(counts.items())),
    }
    snapshot["content_sha256"] = stable_hash(snapshot)
    return snapshot


def validate_idf_snapshot(snapshot: Mapping[str, Any], index: CanonicalIndexHandle) -> None:
    if snapshot.get("rules_sha256") != RULES_SHA256 or snapshot.get("source_snapshot_id") != index.metadata["source_snapshot_id"]:
        raise ValueError("Dedup IDF snapshot uses different rules or source")
    content = {key: value for key, value in snapshot.items() if key != "content_sha256"}
    if stable_hash(content) != snapshot.get("content_sha256"):
        raise ValueError("Dedup IDF snapshot content hash mismatch")


def load_or_build_idf_snapshot(index: CanonicalIndexHandle, source_path: Path) -> dict[str, Any]:
    path = index.cache_dir / f"dedup_idf_v{DEDUP_RULE_VERSION}.json"
    if path.is_file():
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        validate_idf_snapshot(snapshot, index)
        return snapshot
    snapshot = build_idf_snapshot(index, source_path)
    write_json(path, snapshot)
    return snapshot


def weighted_jaccard(left: str, right: str, snapshot: Mapping[str, Any]) -> float:
    a, b = character_grams(left), character_grams(right)
    if not a or not b:
        return 0.0
    count = int(snapshot["document_count"])
    df = snapshot["document_frequency"]
    def weight(gram: str) -> float:
        return math.log((count + 1) / (int(df.get(gram, 0)) + 1)) + 1
    union = a | b
    return sum(weight(gram) for gram in a & b) / sum(weight(gram) for gram in union)


def edit_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    maximum = max(len(left), len(right))
    if not maximum:
        return 1.0
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for column, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (char != other)))
        previous = current
    return 1 - previous[-1] / maximum
