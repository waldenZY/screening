"""Strict one-record-per-request two-stage LLM annotation."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..shared.json_io import JsonObject, load_jsonl, try_parse_json, write_jsonl
from ..shared.llm import LlmCall


STAGE1_FIELDS = frozenset(
    {"category_fit", "semantic_completeness", "intent_clarity", "validity"}
)
STAGE2_FIELDS = frozenset(
    {"classification_difficulty", "task_difficulty", "issue_types", "overall_score"}
)
STAGE1_ENUMS = {
    "category_fit": frozenset({"exact", "partial", "mismatch"}),
    "semantic_completeness": frozenset({"self_contained", "context_dependent", "fragments"}),
    "intent_clarity": frozenset({"clear", "vague", "no_intent"}),
    "validity": frozenset({"valid", "borderline", "invalid"}),
}
CLASSIFICATION_DIFFICULTIES = frozenset(
    {"trivial", "easy", "moderate", "hard", "not_applicable"}
)
TASK_DIFFICULTIES = frozenset(
    {"atomic", "simple", "moderate", "complex", "extreme", "not_applicable"}
)
ISSUE_TYPES = frozenset(
    {
        "none",
        "category_mismatch",
        "ambiguous_intent",
        "context_dependent",
        "truncated",
        "nonsense",
        "emotional_only",
        "too_brief",
        "multi_intent",
    }
)


class QualityResponseError(ValueError):
    """The model returned JSON which violates the frozen stage contract."""


class QualityStageIncomplete(RuntimeError):
    """At least one annotation remained unavailable after all retries."""

    def __init__(self, stage: str, failures: Sequence[str]) -> None:
        self.stage = stage
        self.failures = tuple(failures)
        super().__init__(f"{stage} 存在 {len(failures)} 条工程失败")


@dataclass(frozen=True)
class QualityRunConfig:
    workers: int = 2
    sleep_seconds: float = 1.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        if self.workers < 1 or self.max_retries < 1:
            raise ValueError("workers 和 max_retries 必须是正整数")
        if self.sleep_seconds < 0:
            raise ValueError("sleep_seconds 不能为负数")


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _merge_ledger(path: Path, rows: Sequence[JsonObject], key_name: str) -> None:
    existing: dict[str, JsonObject] = {}
    if path.is_file():
        for row in load_jsonl(path):
            key = str(row.get(key_name, ""))
            if not key or key in existing:
                raise ValueError(f"运行账本 {path.name} 的 {key_name} 非法或重复")
            existing[key] = row
    for row in rows:
        key = str(row[key_name])
        previous = existing.get(key)
        if previous is not None and previous != row:
            raise ValueError(f"运行账本 {path.name} 的冻结任务 {key} 发生变化")
        existing[key] = row
    write_jsonl(path, [existing[key] for key in sorted(existing)])


def _merge_task_ledger(path: Path, rows: Sequence[JsonObject]) -> None:
    """Merge equivalent cache tasks while accumulating newly seen source aliases."""
    existing: dict[str, JsonObject] = {}
    if path.is_file():
        for row in load_jsonl(path):
            key = str(row.get("cache_key", ""))
            if not key or key in existing:
                raise ValueError(f"运行账本 {path.name} 的 cache_key 非法或重复")
            existing[key] = row
    for row in rows:
        key = str(row["cache_key"])
        previous = existing.get(key)
        if previous is None:
            existing[key] = row
            continue
        static_fields = set(row) - {"source_lines", "source_identities"}
        if static_fields != set(previous) - {"source_lines", "source_identities"} or any(
            previous.get(field) != row.get(field) for field in static_fields
        ):
            raise ValueError(f"运行账本 {path.name} 的冻结任务 {key} 发生变化")
        previous["source_lines"] = sorted(
            {int(value) for value in [*previous["source_lines"], *row["source_lines"]]}
        )
        previous["source_identities"] = sorted(
            {str(value) for value in [*previous["source_identities"], *row["source_identities"]]}
        )
    write_jsonl(path, [existing[key] for key in sorted(existing)])


def quality_input(candidate: Mapping[str, Any]) -> JsonObject:
    conversation = candidate["conversation"]
    taxonomy = candidate["taxonomy"]
    return {
        "sample_type": conversation["sample_type"],
        "context_turns": list(conversation["context_turns"]),
        "target_query": conversation["target_query"],
        "L1": taxonomy["l1"],
        "L2": taxonomy["l2"],
    }


def validate_stage1_result(value: Any) -> JsonObject:
    if not isinstance(value, dict) or set(value) != STAGE1_FIELDS:
        raise QualityResponseError("第一阶段响应字段必须与白名单完全一致")
    for key, allowed in STAGE1_ENUMS.items():
        if value.get(key) not in allowed:
            raise QualityResponseError(f"第一阶段 {key} 枚举非法")
    return {key: value[key] for key in sorted(STAGE1_FIELDS)}


def stage1_passes(value: Mapping[str, Any]) -> bool:
    validated = validate_stage1_result(dict(value))
    return validated == {
        "category_fit": "exact",
        "intent_clarity": "clear",
        "semantic_completeness": "self_contained",
        "validity": "valid",
    }


def validate_stage2_result(value: Any, stage1_result: Mapping[str, Any]) -> JsonObject:
    stage1 = validate_stage1_result(dict(stage1_result))
    if not isinstance(value, dict) or set(value) != STAGE2_FIELDS:
        raise QualityResponseError("第二阶段响应字段必须与白名单完全一致")
    classification = value.get("classification_difficulty")
    task = value.get("task_difficulty")
    issues = value.get("issue_types")
    score = value.get("overall_score")
    if classification not in CLASSIFICATION_DIFFICULTIES:
        raise QualityResponseError("第二阶段 classification_difficulty 枚举非法")
    if task not in TASK_DIFFICULTIES:
        raise QualityResponseError("第二阶段 task_difficulty 枚举非法")
    if (
        not isinstance(issues, list)
        or not issues
        or any(not isinstance(item, str) or item not in ISSUE_TYPES for item in issues)
        or len(set(issues)) != len(issues)
        or ("none" in issues and len(issues) != 1)
    ):
        raise QualityResponseError("第二阶段 issue_types 非法")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise QualityResponseError("第二阶段 overall_score 必须是 1~5 整数")

    issue_set = set(issues)
    contradictions = (
        (stage1["category_fit"] == "exact" and "category_mismatch" in issue_set),
        (stage1["semantic_completeness"] == "self_contained" and "context_dependent" in issue_set),
        (stage1["intent_clarity"] == "clear" and "ambiguous_intent" in issue_set),
        (stage1["intent_clarity"] == "clear" and "nonsense" in issue_set),
        (stage1["semantic_completeness"] == "self_contained" and "truncated" in issue_set),
        (stage1["intent_clarity"] == "clear" and "emotional_only" in issue_set),
        (stage1["intent_clarity"] == "clear" and "too_brief" in issue_set),
        (stage1["validity"] == "valid" and classification == "not_applicable"),
        (stage1["validity"] == "valid" and task == "not_applicable"),
    )
    if any(contradictions):
        raise QualityResponseError("第二阶段结果与第一阶段存在逻辑矛盾")
    if score == 5 and issue_set != {"none"}:
        raise QualityResponseError("overall_score=5 时 issue_types 必须为 none")
    if stage1_passes(stage1) and score < 5 and issue_set == {"none"}:
        raise QualityResponseError("满格第一阶段被降分时必须标明 issue_types")
    return {
        "classification_difficulty": classification,
        "task_difficulty": task,
        "issue_types": list(issues),
        "overall_score": score,
    }


class TwoStageQualityRunner:
    """Concurrent runner with a run-local SQLite cache and sanitized ledgers."""

    def __init__(
        self,
        run_root: Path,
        *,
        stage1_prompt: str,
        stage2_prompt: str,
        model: str,
        llm_options: Mapping[str, Any],
        llm_call: LlmCall,
        config: QualityRunConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if stage1_prompt.count("{CANDIDATE_JSON}") != 1:
            raise ValueError("第一阶段 Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
        if stage2_prompt.count("{CANDIDATE_JSON}") != 1:
            raise ValueError("第二阶段 Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
        self.run_root = run_root
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.prompts = {"stage1": stage1_prompt, "stage2": stage2_prompt}
        self.prompt_hashes = {key: _stable_hash(value) for key, value in self.prompts.items()}
        self.model = model
        self.llm_options = dict(llm_options)
        self.options_hash = _stable_hash({"model": model, "options": self.llm_options})
        self.llm_call = llm_call
        self.config = config or QualityRunConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.cache_path = self.run_root / "llm_result_cache.sqlite"
        for stage, prompt in self.prompts.items():
            snapshot = self.run_root / f"{stage}_prompt.md"
            if snapshot.is_file() and snapshot.read_text(encoding="utf-8") != prompt:
                raise ValueError(f"{stage} Prompt 与本次运行已冻结快照不一致")
            if not snapshot.is_file():
                snapshot.write_text(prompt, encoding="utf-8", newline="\n")
        run_contract = {
            "prompt_hashes": self.prompt_hashes,
            "model": self.model,
            "options_sha256": self.options_hash,
            "runner": asdict(self.config),
        }
        contract_path = self.run_root / "quality_run_contract.json"
        if contract_path.is_file():
            existing = json.loads(contract_path.read_text(encoding="utf-8"))
            if existing != run_contract:
                raise ValueError("两阶段 LLM 运行目录与当前 Prompt、模型或配置不兼容")
        else:
            contract_path.write_text(
                json.dumps(run_contract, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        self._initialize_cache()

    def _initialize_cache(self) -> None:
        with sqlite3.connect(self.cache_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    cache_key TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    options_sha256 TEXT NOT NULL,
                    result_json TEXT NOT NULL
                ) WITHOUT ROWID
                """
            )
            check = connection.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise ValueError("两阶段 LLM 运行缓存完整性检查失败")

    def _cache_key(
        self,
        stage: str,
        candidate: Mapping[str, Any],
        stage1_result: Mapping[str, Any] | None,
    ) -> str:
        taxonomy = candidate["taxonomy"]
        return _stable_hash(
            {
                "stage": stage,
                "prompt": self.prompt_hashes[stage],
                "options": self.options_hash,
                "conversation_sha256": candidate["conversation_sha256"],
                "l1": taxonomy["l1"],
                "l2": taxonomy["l2"],
                "stage1": dict(stage1_result) if stage1_result is not None else None,
            }
        )

    def task_id(
        self,
        stage: str,
        candidate: Mapping[str, Any],
        stage1_result: Mapping[str, Any] | None = None,
    ) -> str:
        """Return the frozen task/cache identity recorded in the quality ledgers."""
        if stage not in {"stage1", "stage2"}:
            raise ValueError(f"未知质量阶段: {stage}")
        if stage == "stage1" and stage1_result is not None:
            raise ValueError("第一阶段 task identity 不接受第一阶段结果")
        if stage == "stage2" and stage1_result is None:
            raise ValueError("第二阶段 task identity 必须包含第一阶段结果")
        return self._cache_key(stage, candidate, stage1_result)

    def _load_cached(self, keys: Sequence[str], stage: str) -> dict[str, JsonObject]:
        if not keys:
            return {}
        result: dict[str, JsonObject] = {}
        with sqlite3.connect(self.cache_path) as connection:
            for key in keys:
                row = connection.execute(
                    "SELECT result_json FROM results WHERE cache_key=? AND stage=?",
                    (key, stage),
                ).fetchone()
                if row:
                    result[key] = json.loads(row[0])
        return result

    def _save_cached(self, stage: str, values: Mapping[str, JsonObject]) -> None:
        with sqlite3.connect(self.cache_path) as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO results(
                    cache_key, stage, prompt_sha256, options_sha256, result_json
                ) VALUES(?, ?, ?, ?, ?)
                """,
                [
                    (
                        key,
                        stage,
                        self.prompt_hashes[stage],
                        self.options_hash,
                        json.dumps(value, ensure_ascii=False, sort_keys=True),
                    )
                    for key, value in values.items()
                ],
            )

    def _call(
        self,
        stage: str,
        candidate: Mapping[str, Any],
        stage1_result: Mapping[str, Any] | None,
    ) -> tuple[JsonObject, float, float, str]:
        payload = quality_input(candidate)
        if stage1_result is not None:
            payload.update(validate_stage1_result(dict(stage1_result)))
        prompt = self.prompts[stage].replace(
            "{CANDIDATE_JSON}", json.dumps(payload, ensure_ascii=False, indent=2)
        )
        if self.config.sleep_seconds:
            time.sleep(self.config.sleep_seconds)
        raw, first_latency, full_latency = self.llm_call(
            [{"role": "user", "content": prompt}],
            model=self.model,
            max_tokens=int(self.llm_options["max_tokens"]),
            stream=bool(self.llm_options["stream"]),
            enable_thinking=bool(self.llm_options["enable_thinking"]),
            skip_special_tokens=bool(self.llm_options["skip_special_tokens"]),
            timeout=float(self.llm_options["timeout"]),
        )
        success, parsed = try_parse_json(raw)
        if not success:
            raise QualityResponseError("响应不是合法 JSON 对象")
        result = (
            validate_stage1_result(parsed)
            if stage == "stage1"
            else validate_stage2_result(parsed, stage1_result or {})
        )
        return result, first_latency, full_latency, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _run(
        self,
        stage: str,
        candidates: Sequence[JsonObject],
        stage1_by_identity: Mapping[str, JsonObject] | None = None,
    ) -> dict[str, JsonObject]:
        by_key: dict[str, list[JsonObject]] = defaultdict(list)
        stage1_for_key: dict[str, JsonObject | None] = {}
        for candidate in candidates:
            identity = candidate["source_identity_sha256"]
            stage1 = stage1_by_identity[identity] if stage1_by_identity is not None else None
            key = self._cache_key(stage, candidate, stage1)
            by_key[key].append(candidate)
            stage1_for_key[key] = stage1
        cached = self._load_cached(list(by_key), stage)
        for matching_key, value in cached.items():
            if stage == "stage1":
                validate_stage1_result(value)
            else:
                validate_stage2_result(value, stage1_for_key[matching_key] or {})

        tasks = []
        for key, grouped in sorted(by_key.items()):
            representative = grouped[0]
            tasks.append(
                {
                    "cache_key": key,
                    "stage": stage,
                    "source_lines": sorted(int(item["source"]["source_line"]) for item in grouped),
                    "source_identities": sorted(item["source_identity_sha256"] for item in grouped),
                    "conversation_sha256": representative["conversation_sha256"],
                    "taxonomy_sha256": _stable_hash(representative["taxonomy"]),
                    "prompt_sha256": self.prompt_hashes[stage],
                    "options_sha256": self.options_hash,
                }
            )
        _merge_task_ledger(self.run_root / f"{stage}_tasks.jsonl", tasks)

        pending = [key for key in by_key if key not in cached]
        failures: dict[str, str] = {}
        attempt_path = self.run_root / f"{stage}_attempts.jsonl"
        attempt_lock = threading.Lock()
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        with attempt_path.open("a", encoding="utf-8", newline="\n") as attempt_file:
            for attempt_no in range(1, self.config.max_retries + 1):
                if not pending:
                    break
                failures = {}
                successes: dict[str, JsonObject] = {}
                with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
                    futures = {
                        executor.submit(
                            self._call,
                            stage,
                            by_key[key][0],
                            stage1_for_key[key],
                        ): key
                        for key in pending
                    }
                    for future in as_completed(futures):
                        key = futures[future]
                        attempt: JsonObject = {
                            "cache_key": key,
                            "stage": stage,
                            "attempt_no": attempt_no,
                            "finished_at": _utc_now(),
                        }
                        try:
                            result, first_latency, full_latency, output_hash = future.result()
                            successes[key] = result
                            attempt.update(
                                {
                                    "outcome": "success",
                                    "output_sha256": output_hash,
                                    "first_token_latency_ms": first_latency,
                                    "full_latency_ms": full_latency,
                                    "error": None,
                                }
                            )
                        except Exception as exc:  # transport and contract failures are both retried
                            failures[key] = f"{type(exc).__name__}: {exc}"
                            attempt.update(
                                {
                                    "outcome": "failure",
                                    "output_sha256": None,
                                    "first_token_latency_ms": None,
                                    "full_latency_ms": None,
                                    "error": failures[key],
                                }
                            )
                        with attempt_lock:
                            attempt_file.write(json.dumps(attempt, ensure_ascii=False) + "\n")
                            attempt_file.flush()
                if successes:
                    self._save_cached(stage, successes)
                    cached.update(successes)
                pending = [key for key in pending if key not in successes]
                if pending and attempt_no < self.config.max_retries:
                    time.sleep(min(2**attempt_no, 10))
        if pending:
            raise QualityStageIncomplete(stage, [f"{key}:{failures.get(key, 'unknown')}" for key in pending])

        cache_results = [
            {"cache_key": key, "stage": stage, "result": cached[key]}
            for key in sorted(by_key)
        ]
        _merge_ledger(self.run_root / f"{stage}_results.jsonl", cache_results, "cache_key")
        result_by_identity: dict[str, JsonObject] = {}
        for key, grouped in by_key.items():
            for candidate in grouped:
                result_by_identity[candidate["source_identity_sha256"]] = dict(cached[key])
        return result_by_identity

    def run_stage1(self, candidates: Sequence[JsonObject]) -> dict[str, JsonObject]:
        return self._run("stage1", candidates)

    def run_stage2(
        self,
        candidates: Sequence[JsonObject],
        stage1_by_identity: Mapping[str, JsonObject],
    ) -> dict[str, JsonObject]:
        return self._run("stage2", candidates, stage1_by_identity)


def run_stage1(
    candidate: JsonObject,
    *,
    runner: TwoStageQualityRunner,
) -> JsonObject:
    """Annotate one candidate with the first-stage contract."""
    return runner.run_stage1([candidate])[candidate["source_identity_sha256"]]


def run_stage2(
    candidate: JsonObject,
    stage1_result: JsonObject,
    *,
    runner: TwoStageQualityRunner,
) -> JsonObject:
    """Annotate one admitted candidate with the second-stage contract."""
    identity = candidate["source_identity_sha256"]
    return runner.run_stage2([candidate], {identity: stage1_result})[identity]
