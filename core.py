"""Shared constants and I/O helpers for the filtering pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

QC_DECISIONS = {"KEEP", "REVIEW", "REJECT"}
REVIEW_CLASSES = {"TESTABLE_DIFFICULTY", "UNUSABLE"}
QC_CHECK_FIELDS = {
    "has_user_goal",
    "goal_identifiable",
    "context_sufficient",
    "testable",
}
VALID_REASONS = {
    "VALID_ACTION",
    "VALID_INFORMATION_REQUEST",
    "VALID_CONVERSATION",
    "VALID_CONTENT_GENERATION",
    "VALID_FOLLOWUP",
    "NORMAL_CLARIFICATION_NEEDED",
    "NOISY_BUT_USABLE_CONTEXT",
}
TESTABLE_DIFFICULTY_REASONS = {
    "AMBIGUOUS_INTENT",
    "CONTEXT_DEPENDENCY",
    "UNRESOLVED_REFERENCE",
    "DISAMBIGUATION",
    "MULTI_INTENT",
}
UNUSABLE_REASONS = {
    "NO_USER_GOAL",
    "STATUS_ONLY",
    "ENTITY_ONLY",
    "ACK_ONLY",
    "ASR_GARBLED",
    "FRAGMENTED_TEXT",
    "MISSING_CRITICAL_CONTEXT",
    "MISSING_ACTION_OBJECT",
    "NOT_TESTABLE",
}
QC_REASON_CODES = VALID_REASONS | TESTABLE_DIFFICULTY_REASONS | UNUSABLE_REASONS

SET_FILES: dict[str, Path] = {
    "P0": Path("P0-冒烟集") / "P0_smoke_set_v4_labeled.jsonl",
    "P1": Path("P1-基准集") / "P1_benchmark_set_v4_labeled.jsonl",
    "P2": Path("P2-对抗集") / "P2_adversarial_set_v4_labeled.jsonl",
}

DATASET_PREFIXES: dict[str, str] = {
    set_name: path.stem.removesuffix("_labeled")
    for set_name, path in SET_FILES.items()
}

PRESCREEN_FILES: dict[str, Path] = {
    set_name: path.with_name(f"{DATASET_PREFIXES[set_name]}_prescreen.jsonl")
    for set_name, path in SET_FILES.items()
}

SUPPLEMENT_FILES: dict[str, Path] = {
    set_name: path.with_name(f"{DATASET_PREFIXES[set_name]}_supplement.jsonl")
    for set_name, path in SET_FILES.items()
}

SCREENED_FILES: dict[str, Path] = {
    set_name: path.with_name(f"{DATASET_PREFIXES[set_name]}_screened.jsonl")
    for set_name, path in SET_FILES.items()
}


def set_workspace(run_root: Path, set_name: str) -> Path:
    """Return the isolated workspace for one dataset branch."""
    if set_name not in SET_FILES:
        raise ValueError(f"未知数据集: {set_name}")
    return run_root / set_name


def prescreen_attempts_path(run_root: Path, set_name: str) -> Path:
    return set_workspace(run_root, set_name) / "prescreen_attempts.jsonl"


def prescreen_results_path(run_root: Path, set_name: str) -> Path:
    return set_workspace(run_root, set_name) / "prescreen_results.jsonl"


def supplement_artifact_path(run_root: Path, filename: str) -> Path:
    return set_workspace(run_root, "P1") / filename


def run_prescreen_dataset_path(run_root: Path, set_name: str) -> Path:
    return set_workspace(run_root, set_name) / PRESCREEN_FILES[set_name].name


def run_supplement_dataset_path(run_root: Path, set_name: str) -> Path:
    return set_workspace(run_root, set_name) / SUPPLEMENT_FILES[set_name].name


def run_screened_dataset_path(run_root: Path, set_name: str) -> Path:
    return set_workspace(run_root, set_name) / SCREENED_FILES[set_name].name


def parse_csv(value: str) -> list[str]:
    """Split a comma-separated CLI value and discard empty items."""
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_scores(value: str) -> set[int]:
    """Parse a non-empty comma-separated set of quality scores from 1 to 5."""
    scores: set[int] = set()
    for item in parse_csv(value):
        try:
            score = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"非法分数: {item!r}") from exc
        if not 1 <= score <= 5:
            raise argparse.ArgumentTypeError("分数必须在 1～5 之间")
        scores.add(score)
    if not scores:
        raise argparse.ArgumentTypeError("至少指定一个分数")
    return scores


def positive_int(value: str) -> int:
    """Parse a strictly positive integer CLI value."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"必须是整数: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须大于等于 1")
    return parsed


def non_negative_int(value: str) -> int:
    """Parse a non-negative integer CLI value."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"必须是整数: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def non_negative_float(value: str) -> float:
    """Parse a non-negative floating-point CLI value."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"必须是数字: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def percentage(value: str) -> float:
    """Parse a percentage in the inclusive range 0 to 100."""
    parsed = non_negative_float(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("百分比不能大于 100")
    return parsed


def validate_selected_sets(value: str) -> list[str]:
    """Parse and validate selected dataset names."""
    selected_sets = list(dict.fromkeys(parse_csv(value.upper())))
    if not selected_sets:
        raise ValueError("至少指定一个数据集")
    unknown_sets = [name for name in selected_sets if name not in SET_FILES]
    if unknown_sets:
        raise ValueError(f"未知数据集: {', '.join(unknown_sets)}")
    return selected_sets


def load_prompt_template(path: Path) -> str:
    """Load a prompt template containing exactly one candidate placeholder."""
    template = path.read_text(encoding="utf-8")
    placeholder = "{CANDIDATE_JSON}"
    if template.count(placeholder) != 1:
        raise ValueError(f"Prompt 必须且只能包含一个 {placeholder} 占位符")
    return template


def split_turns(value: Any) -> list[str]:
    """Normalize either a list of turns or a pipe-delimited string."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(" | ") if part.strip()]


def build_candidate(row: JsonObject) -> JsonObject:
    """Build the normalized query candidate sent to the LLM."""
    context_turns = split_turns(row.get("context_turns", row.get("context_text")))
    target_query = str(row.get("target_query") or "").strip()
    if not target_query:
        full_turns = split_turns(row.get("text", row.get("raw_text")))
        if full_turns:
            target_query = full_turns[-1]
            if not context_turns:
                context_turns = full_turns[:-1]

    sample_type = str(row.get("sample_type") or "").strip()
    if sample_type not in {"single_turn", "multi_turn"}:
        sample_type = "multi_turn" if context_turns else "single_turn"

    return {
        "sample_type": sample_type,
        "context_turns": context_turns,
        "target_query": target_query,
    }


def build_cluster_candidate(row: JsonObject) -> JsonObject:
    """Build a candidate from an upstream clustering record."""
    target_query = str(row.get("raw_query") or "").strip()
    context_turns = split_turns(row.get("text"))
    if target_query and context_turns and context_turns[-1] == target_query:
        context_turns = context_turns[:-1]
    if not target_query:
        return build_candidate(row)
    return {
        "sample_type": "multi_turn" if context_turns else "single_turn",
        "context_turns": context_turns,
        "target_query": target_query,
    }


def iter_jsonl(path: Path) -> Iterator[tuple[int, JsonObject]]:
    """Yield 1-based physical line numbers and JSON objects from a JSONL file."""
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


def load_labeled_by_line(path: Path) -> dict[int, JsonObject]:
    """Index labeled records by their 1-based physical source line."""
    return {line_number: record for line_number, record in iter_jsonl(path)}


def get_decision(record: JsonObject) -> str:
    """Return the normalized LLM decision stored in a result record."""
    parsed_result = record.get("parsed_result") or {}
    if not isinstance(parsed_result, dict):
        return "UNKNOWN"
    return str(parsed_result.get("decision", "UNKNOWN")).upper()


def validate_qc_result(value: Any) -> tuple[bool, JsonObject | None, str | None]:
    """Validate and normalize the unified quality-control response."""
    if not isinstance(value, dict):
        return False, None, "响应必须是 JSON 对象"
    decision = str(value.get("decision", "")).upper()
    if decision not in QC_DECISIONS:
        return False, None, "decision 必须是 KEEP、REVIEW 或 REJECT"

    checks = value.get("checks")
    if not isinstance(checks, dict) or set(checks) != QC_CHECK_FIELDS:
        return False, None, "checks 必须且只能包含四个规定字段"
    if any(type(checks[name]) is not bool for name in QC_CHECK_FIELDS):
        return False, None, "checks 中的字段必须是 JSON 布尔值"

    interpreted_goal = value.get("interpreted_goal")
    if interpreted_goal is not None and not isinstance(interpreted_goal, str):
        return False, None, "interpreted_goal 必须是字符串或 null"
    if isinstance(interpreted_goal, str):
        interpreted_goal = interpreted_goal.strip() or None

    raw_reasons = value.get("reason_codes")
    if not isinstance(raw_reasons, list) or not raw_reasons:
        return False, None, "reason_codes 必须是非空数组"
    reason_codes = [str(item).upper() for item in raw_reasons]
    if len(set(reason_codes)) != len(reason_codes):
        return False, None, "reason_codes 不得重复"
    if any(reason not in QC_REASON_CODES for reason in reason_codes):
        return False, None, "reason_codes 包含非法枚举值"

    confidence = value.get("judge_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return False, None, "judge_confidence 必须是 0 到 1 的数值"
    if not 0 <= float(confidence) <= 1:
        return False, None, "judge_confidence 必须是 0 到 1 的数值"

    short_reason = value.get("short_reason")
    if not isinstance(short_reason, str) or not short_reason.strip():
        return False, None, "short_reason 必须是非空字符串"
    short_reason = short_reason.strip()
    if len(short_reason) > 80:
        return False, None, "short_reason 不能超过 80 个字符"

    review_class_value = value.get("review_class")
    review_class = (
        str(review_class_value).upper()
        if review_class_value is not None and str(review_class_value).strip()
        else None
    )
    if decision == "REVIEW":
        if review_class not in REVIEW_CLASSES:
            return False, None, "REVIEW 必须提供合法 review_class"
        allowed_reasons = (
            TESTABLE_DIFFICULTY_REASONS
            if review_class == "TESTABLE_DIFFICULTY"
            else UNUSABLE_REASONS
        )
        if any(reason not in allowed_reasons for reason in reason_codes):
            return False, None, "review_class 与 reason_codes 不匹配"
        if review_class == "TESTABLE_DIFFICULTY" and (
            not checks["has_user_goal"]
            or not checks["context_sufficient"]
            or not checks["testable"]
        ):
            return False, None, "TESTABLE_DIFFICULTY 必须有用户目标、上下文充分且可测试"
        if review_class == "UNUSABLE" and checks["testable"]:
            return False, None, "UNUSABLE REVIEW 的 testable 必须为 false"
    elif review_class is not None:
        return False, None, "非 REVIEW 结果不得设置 review_class"

    if decision == "KEEP":
        if any(reason not in VALID_REASONS for reason in reason_codes):
            return False, None, "KEEP 的 reason_codes 非法"
        if not all(checks[name] for name in QC_CHECK_FIELDS):
            return False, None, "KEEP 的四项 checks 必须全部为 true"
    if decision == "REJECT":
        if any(reason not in UNUSABLE_REASONS for reason in reason_codes):
            return False, None, "REJECT 的 reason_codes 非法"
        if all(checks[name] for name in QC_CHECK_FIELDS):
            return False, None, "REJECT 至少需要一项 check 为 false"
    if not checks["has_user_goal"] and interpreted_goal is not None:
        return False, None, "没有用户目标时 interpreted_goal 必须为 null"
    if checks["goal_identifiable"] and interpreted_goal is None:
        return False, None, "目标可识别时 interpreted_goal 不能为空"

    normalized: JsonObject = {
        "decision": decision,
        "review_class": review_class,
        "checks": {name: checks[name] for name in sorted(QC_CHECK_FIELDS)},
        "interpreted_goal": interpreted_goal,
        "reason_codes": reason_codes,
        "judge_confidence": float(confidence),
        "short_reason": short_reason,
    }
    return True, normalized, None


def is_testable_difficulty(record: JsonObject) -> bool:
    """Return whether a canonical result is a retainable P2 REVIEW."""
    parsed = record.get("parsed_result")
    return (
        isinstance(parsed, dict)
        and str(parsed.get("decision", "")).upper() == "REVIEW"
        and str(parsed.get("review_class", "")).upper() == "TESTABLE_DIFFICULTY"
    )


def query_text(record: JsonObject) -> str:
    """Extract the query text used by cross-source identity matching."""
    direct = str(record.get("target_query") or record.get("raw_query") or "").strip()
    if direct:
        return direct
    text = record.get("text", record.get("raw_text"))
    turns = split_turns(text)
    return turns[-1] if turns else str(text or "").strip()


def sample_identity(record: JsonObject) -> tuple[str, str]:
    """Build the deduplication key used by the taxonomy extraction script."""
    session = record.get("session_id")
    if session is None or str(session).strip() == "":
        session = record.get("session", "")
    identity_text = record.get("text")
    if identity_text is None or str(identity_text).strip() == "":
        identity_text = record.get("target_query")
    if identity_text is None or str(identity_text).strip() == "":
        identity_text = record.get("raw_query", "")
    return str(session or "").strip(), str(identity_text or "").strip()


def stable_sample_rank(seed: int, namespace: str, record: JsonObject) -> str:
    """Return a deterministic pseudo-random rank for a sample."""
    return stable_hash(
        {
            "seed": seed,
            "namespace": namespace,
            "identity": sample_identity(record),
            "record": record,
        }
    )


def record_fingerprint(record: JsonObject) -> str:
    """Return a stable representation used to match copied original records."""
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def try_parse_json(raw_output: str) -> tuple[bool, JsonObject | None]:
    """Parse a JSON object returned directly or inside a Markdown code fence."""
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
    """Atomically write JSON objects to a UTF-8 JSONL file."""
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
    """Atomically write a JSON object with stable, readable formatting."""
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


def write_text(path: Path, value: str) -> None:
    """Atomically write UTF-8 text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for a file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: Any, length: int = 64) -> str:
    """Hash a JSON-serializable value using a deterministic representation."""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()[:length]


def ensure_within(path: Path, root: Path, label: str = "路径") -> Path:
    """Resolve a path and reject values outside the declared root directory."""
    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label}必须位于 {resolved_root} 内: {resolved_path}") from exc
    return resolved_path


def validate_source_run_roots(source_root: Path, run_root: Path) -> tuple[Path, Path]:
    """Resolve roots and prevent a run workspace from writing inside the source."""
    resolved_source = source_root.expanduser().resolve()
    resolved_run = run_root.expanduser().resolve()
    if resolved_run == resolved_source or resolved_run.is_relative_to(resolved_source):
        raise ValueError("run_root 不能等于或位于 source_root 内部")
    return resolved_source, resolved_run
