"""Auditable, resumable LLM batch execution shared by filtering stages."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .core import (
    JsonObject,
    get_decision,
    load_jsonl,
    try_parse_json,
    validate_qc_result,
    write_jsonl,
)
from .shared import chat_completion

TERMINAL_DECISIONS = {"KEEP", "REJECT", "REVIEW"}
LlmCall = Callable[..., tuple[str, float, float]]
ResultValidator = Callable[[Any], tuple[bool, JsonObject | None, str | None]]
AttemptSink = Callable[[JsonObject], None]


@dataclass(frozen=True)
class BatchOutcome:
    expected: int
    already_completed: int
    completed_now: int
    parse_failures: int
    engineering_failures: tuple[str, ...]
    fatal_error: str | None

    @property
    def complete(self) -> bool:
        return not self.engineering_failures and self.fatal_error is None


def validate_llm_environment() -> None:
    """Fail before scheduling calls when required endpoint configuration is absent."""
    for name in ("LLM_API_URL", "LLM_API_KEY"):
        value = os.getenv(name, "")
        if not value or value == "xxxx":
            raise RuntimeError(f"缺少配置：请设置环境变量 {name}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _classify_exception(exc: Exception) -> tuple[str, bool]:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        if 400 <= status_code < 500 and status_code not in {408, 409, 425, 429}:
            return f"http_{status_code}", True
        if status_code == 429:
            return "rate_limit", False
        if status_code >= 500:
            return "server_error", False
    message = str(exc).lower()
    if "llm_api_" in message or "缺少配置" in message:
        return "configuration", True
    class_name = type(exc).__name__.lower()
    if "timeout" in class_name or "timeout" in message:
        return "timeout", False
    if "connection" in class_name or "connection" in message:
        return "connection", False
    return "call_error", False


def _attempt_counts(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not path.is_file():
        return counts
    for record in load_jsonl(path):
        filter_id = record.get("filter_id")
        attempt_no = record.get("attempt_no")
        if isinstance(filter_id, str) and type(attempt_no) is int:
            counts[filter_id] = max(counts.get(filter_id, 0), attempt_no)
    return counts


def _canonical_by_id(path: Path) -> dict[str, JsonObject]:
    canonical: dict[str, JsonObject] = {}
    if not path.is_file():
        return canonical
    for record in load_jsonl(path):
        filter_id = record.get("filter_id")
        if isinstance(filter_id, str):
            canonical[filter_id] = record
    return canonical


def _call_record(
    record: JsonObject,
    stage: str,
    model: str,
    max_retries: int,
    sleep_sec: float,
    llm_options: JsonObject,
    first_attempt_no: int,
    llm_call: LlmCall,
    result_validator: ResultValidator,
    attempt_sink: AttemptSink,
    stop_event: threading.Event,
) -> tuple[list[JsonObject], JsonObject | None, str | None, bool]:
    if stop_event.is_set():
        return [], None, "因全局不可重试错误取消", False
    if sleep_sec:
        time.sleep(sleep_sec)
    attempts: list[JsonObject] = []
    messages = [{"role": "user", "content": record["prompt"]}]
    for local_attempt in range(max_retries):
        if stop_event.is_set():
            return attempts, None, "因全局不可重试错误取消", False
        attempt_no = first_attempt_no + local_attempt
        started_at = _utc_now()
        try:
            raw_output, first_latency, full_latency = llm_call(
                messages,
                model=model,
                max_tokens=int(llm_options["max_tokens"]),
                stream=bool(llm_options["stream"]),
                enable_thinking=bool(llm_options["enable_thinking"]),
                skip_special_tokens=bool(llm_options["skip_special_tokens"]),
                timeout=float(llm_options["timeout"]),
            )
            if not raw_output:
                raise RuntimeError("LLM 返回空结果")
            json_success, parsed_value = try_parse_json(raw_output)
            valid_result, parsed_result, validation_error = (
                result_validator(parsed_value)
                if json_success
                else (False, None, "响应不是合法 JSON 对象")
            )
            decision = (
                get_decision({"parsed_result": parsed_result})
                if valid_result
                else "UNKNOWN"
            )
            valid_result = valid_result and decision in TERMINAL_DECISIONS
            outcome = "success" if valid_result else "parse_fail"
            attempt = {
                "attempt_id": uuid.uuid4().hex,
                "stage": stage,
                "filter_id": record["filter_id"],
                "set": record["set"],
                "source_line": record["source_line"],
                "attempt_no": attempt_no,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "model": model,
                "outcome": outcome,
                "raw_output": raw_output,
                "parse_success": valid_result,
                "parsed_result": parsed_result if valid_result else None,
                "first_token_latency_ms": first_latency,
                "full_latency_ms": full_latency,
                "error_category": None if valid_result else "parse_error",
                "error": None if valid_result else validation_error,
            }
            attempts.append(attempt)
            attempt_sink(attempt)
            if not valid_result:
                if local_attempt + 1 < max_retries:
                    time.sleep(min(2 ** (local_attempt + 1), 10))
                    continue
                return attempts, None, validation_error or "模型响应无法解析", False
            canonical = {
                key: value for key, value in record.items() if key != "prompt"
            }
            canonical.update(
                {
                    "outcome": "success",
                    "raw_output": raw_output,
                    "parse_success": True,
                    "parsed_result": parsed_result,
                    "first_token_latency_ms": first_latency,
                    "full_latency_ms": full_latency,
                    "attempts": attempt_no,
                    "error": None,
                }
            )
            return attempts, canonical, None, False
        except Exception as exc:
            category, fatal = _classify_exception(exc)
            failed_attempt = {
                    "attempt_id": uuid.uuid4().hex,
                    "stage": stage,
                    "filter_id": record["filter_id"],
                    "set": record["set"],
                    "source_line": record["source_line"],
                    "attempt_no": attempt_no,
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "model": model,
                    "outcome": "call_error",
                    "raw_output": None,
                    "parse_success": False,
                    "parsed_result": None,
                    "first_token_latency_ms": None,
                    "full_latency_ms": None,
                    "error_category": category,
                    "error": str(exc),
                }
            attempts.append(failed_attempt)
            attempt_sink(failed_attempt)
            if fatal:
                stop_event.set()
                return attempts, None, str(exc), True
            if local_attempt + 1 < max_retries:
                time.sleep(min(2 ** (local_attempt + 1), 10))
    return attempts, None, attempts[-1]["error"], False


def run_inference_batch(
    records: list[JsonObject],
    *,
    stage: str,
    attempts_path: Path,
    results_path: Path,
    model: str,
    workers: int,
    sleep_sec: float,
    max_retries: int,
    llm_options: JsonObject,
    selected_sets: list[str],
    logger: logging.Logger,
    llm_call: LlmCall = chat_completion,
    result_validator: ResultValidator = validate_qc_result,
) -> BatchOutcome:
    """Run pending records, append attempts, and atomically materialize terminals."""
    canonical = _canonical_by_id(results_path)
    for filter_id, existing in canonical.items():
        valid, _, error = result_validator(existing.get("parsed_result"))
        if existing.get("parse_success") is not True or not valid:
            raise ValueError(f"已有规范结果 {filter_id} 非法: {error or 'parse_success 非 true'}")
    expected_by_id = {str(record["filter_id"]): record for record in records}
    if len(expected_by_id) != len(records):
        raise ValueError("推理任务存在重复 filter_id")
    expected_ids = set(expected_by_id)
    for filter_id in expected_ids & set(canonical):
        existing = canonical[filter_id]
        expected = expected_by_id[filter_id]
        if any(
            existing.get(key) != value
            for key, value in expected.items()
            if key != "prompt"
        ):
            raise ValueError(f"已有规范结果 {filter_id} 与当前冻结任务不一致")
    pending = [record for record in records if record["filter_id"] not in canonical]
    already_completed = len(expected_ids & set(canonical))
    counts = _attempt_counts(attempts_path)
    terminal_results: list[JsonObject] = []
    engineering_failures: list[str] = []
    fatal_error: str | None = None
    parse_failures = 0
    stop_event = threading.Event()

    attempts_path.parent.mkdir(parents=True, exist_ok=True)
    with attempts_path.open("a", encoding="utf-8", newline="\n") as attempt_file:
        attempt_lock = threading.Lock()

        def append_attempt(attempt: JsonObject) -> None:
            with attempt_lock:
                attempt_file.write(json.dumps(attempt, ensure_ascii=False) + "\n")
                attempt_file.flush()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _call_record,
                    record,
                    stage,
                    model,
                    max_retries,
                    sleep_sec,
                    llm_options,
                    counts.get(str(record["filter_id"]), 0) + 1,
                    llm_call,
                    result_validator,
                    append_attempt,
                    stop_event,
                ): record
                for record in pending
            }
            completed = 0
            for future in as_completed(futures):
                source_record = futures[future]
                if future.cancelled():
                    engineering_failures.append(str(source_record["filter_id"]))
                    continue
                attempts, terminal, error, fatal = future.result()
                for attempt in attempts:
                    if attempt["outcome"] == "parse_fail":
                        parse_failures += 1
                completed += 1
                filter_id = str(source_record["filter_id"])
                if terminal is not None:
                    terminal_results.append(terminal)
                    decision = get_decision(terminal)
                    logger.info(
                        "[%d/%d] %s %s",
                        completed,
                        len(pending),
                        filter_id,
                        decision,
                    )
                else:
                    engineering_failures.append(filter_id)
                    logger.error("[%d/%d] %s 调用失败: %s", completed, len(pending), filter_id, error)
                if fatal and fatal_error is None:
                    fatal_error = error or "不可重试的模型调用错误"
                    for pending_future in futures:
                        if pending_future is not future:
                            pending_future.cancel()

    for result in terminal_results:
        canonical[str(result["filter_id"])] = result
    set_order = {name: index for index, name in enumerate(selected_sets)}
    ordered = sorted(
        canonical.values(),
        key=lambda item: (
            set_order.get(str(item.get("set")), len(set_order)),
            int(item.get("source_line", 0)),
            str(item.get("filter_id", "")),
        ),
    )
    write_jsonl(results_path, ordered)
    return BatchOutcome(
        expected=len(expected_ids),
        already_completed=already_completed,
        completed_now=len(terminal_results),
        parse_failures=parse_failures,
        engineering_failures=tuple(engineering_failures),
        fatal_error=fatal_error,
    )
