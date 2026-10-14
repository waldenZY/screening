"""Strict one-record-per-request two-stage LLM annotation."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..shared.json_io import JsonObject, load_jsonl, try_parse_json, write_jsonl
from ..shared.challenge import CHALLENGE_TYPES
from ..shared.llm import LlmCall
from ..candidate.contract import parse_conversation


STAGE1_FIELDS = frozenset(
    {"category_fit", "semantic_completeness", "intent_clarity", "validity", "blocking_issues"}
)
STAGE1_BLOCKING_ISSUES = (
    "attachment_dependency", "missing_required_input", "unresolved_reference",
    "no_evaluable_goal", "unrecoverable_text", "unclear_evaluation_boundary",
)
STAGE2_FIELDS = frozenset(
    {"task_difficulty", "intent_label", "execution_issues"}
)
INTENT_LABELS = frozenset({"task", "qa"})
EXECUTION_ISSUES = frozenset({"no_evaluable_goal", "attachment_dependency", "unresolved_reference", "other_unfit"})
CHALLENGE_FIELDS = frozenset(
    {"is_challenge", "primary_challenge", "challenge_types"}
)
CHALLENGE_TYPE_SET = frozenset(CHALLENGE_TYPES)
STAGE1_ENUMS = {
    "category_fit": frozenset({"exact", "partial", "mismatch"}),
    "semantic_completeness": frozenset({"self_contained", "context_dependent", "fragments"}),
    "intent_clarity": frozenset({"clear", "vague", "no_intent"}),
    "validity": frozenset({"valid", "borderline", "invalid"}),
}
TASK_DIFFICULTIES = frozenset(
    {"atomic", "simple", "moderate", "complex", "extreme", "not_applicable"}
)
QUALITY_CONTRACT_VERSION = 9


class QualityResponseError(ValueError):
    """The model returned JSON which violates the frozen stage contract."""


class QualityStageIncomplete(RuntimeError):
    """At least one annotation remained unavailable after all retries."""

    def __init__(self, stage: str, failures: Sequence[str]) -> None:
        self.stage = stage
        self.failures = tuple(failures)
        super().__init__(f"{stage} 存在 {len(failures)} 条工程失败")


class _FairRequestGate:
    """Bound total LLM concurrency and rotate capacity across active branches."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._condition = threading.Condition()
        self._in_flight = 0
        self._waiting: dict[str, deque[object]] = {}
        self._branches: deque[str] = deque()

    def acquire(self, branch: str) -> None:
        token = object()
        with self._condition:
            queue = self._waiting.setdefault(branch, deque())
            if not queue:
                self._branches.append(branch)
            queue.append(token)
            while (
                self._in_flight >= self.capacity
                or not self._branches
                or self._branches[0] != branch
                or self._waiting[branch][0] is not token
            ):
                self._condition.wait()
            queue.popleft()
            self._in_flight += 1
            self._branches.popleft()
            if queue:
                self._branches.append(branch)
            else:
                self._waiting.pop(branch, None)
            self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._in_flight -= 1
            self._condition.notify_all()


def _sanitized_error(exc: Exception) -> str:
    """Keep task ledgers useful without persisting provider/request content."""
    if isinstance(exc, QualityResponseError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


@dataclass(frozen=True)
class QualityRunConfig:
    workers: int = 10
    sleep_seconds: float = 1.0
    max_retries: int = 20

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


def stage2_quality_input(candidate: Mapping[str, Any]) -> JsonObject:
    """Describe the whole ordered sample without labels or upstream judgments."""
    conversation = candidate["conversation"]
    parsed = parse_conversation(conversation["full_text"])
    if conversation["sample_type"] != parsed["sample_type"]:
        raise ValueError("Stage2 sample_type 与完整会话轮次不一致")
    return {
        "sample_type": parsed["sample_type"],
        "conversation_turns": parsed["full_text"].split(" | "),
    }


def stage1_quality_input(candidate: Mapping[str, Any]) -> JsonObject:
    """Preserve every turn; isolate the candidate category used only for checking."""
    payload = stage2_quality_input(candidate)
    taxonomy = candidate["taxonomy"]
    return {
        **payload,
        "candidate_category": {"L1": taxonomy["l1"], "L2": taxonomy["l2"]},
    }


def stage1_input_sha256(candidate: Mapping[str, Any]) -> str:
    return _stable_hash(stage1_quality_input(candidate))


def stage1_task_key(
    candidate: Mapping[str, Any], *, prompt_sha256: str, options_sha256: str,
) -> str:
    return _stable_hash({
        "stage": "stage1", "prompt": prompt_sha256, "options": options_sha256,
        "input_sha256": stage1_input_sha256(candidate),
    })


def stage2_input_sha256(candidate: Mapping[str, Any]) -> str:
    return _stable_hash(stage2_quality_input(candidate))


def stage2_task_key(
    candidate: Mapping[str, Any], *, prompt_sha256: str, options_sha256: str,
) -> str:
    """Freeze precisely the content sent to Stage2 and its inference settings."""
    return _stable_hash({
        "stage": "stage2",
        "prompt": prompt_sha256,
        "options": options_sha256,
        "input_sha256": stage2_input_sha256(candidate),
    })


def validate_stage1_result(value: Any) -> JsonObject:
    if not isinstance(value, dict) or set(value) != STAGE1_FIELDS:
        raise QualityResponseError("第一阶段响应字段必须与白名单完全一致")
    for key, allowed in STAGE1_ENUMS.items():
        if not isinstance(value.get(key), str) or value[key] not in allowed:
            raise QualityResponseError(f"第一阶段 {key} 枚举非法")
    issues = value["blocking_issues"]
    if (
        not isinstance(issues, list)
        or any(not isinstance(item, str) or item not in STAGE1_BLOCKING_ISSUES for item in issues)
        or len(issues) != len(set(issues))
    ):
        raise QualityResponseError("第一阶段 blocking_issues 非法")
    if issues and value["validity"] == "valid":
        raise QualityResponseError("有 blocking_issues 时 validity 不能为 valid")
    if set(issues) & {"attachment_dependency", "missing_required_input", "unresolved_reference"}:
        if value["semantic_completeness"] != "context_dependent":
            raise QualityResponseError("缺关键输入时 semantic_completeness 必须为 context_dependent")
    if "unrecoverable_text" in issues and value["semantic_completeness"] != "fragments":
        raise QualityResponseError("unrecoverable_text 必须对应 fragments")
    if "no_evaluable_goal" in issues and value["intent_clarity"] != "no_intent":
        raise QualityResponseError("no_evaluable_goal 必须对应 no_intent")
    return {
        **{key: value[key] for key in sorted(STAGE1_ENUMS)},
        "blocking_issues": [key for key in STAGE1_BLOCKING_ISSUES if key in issues],
    }


def stage1_passes(value: Mapping[str, Any]) -> bool:
    validated = validate_stage1_result(dict(value))
    return validated == {
        "category_fit": "exact",
        "intent_clarity": "clear",
        "semantic_completeness": "self_contained",
        "validity": "valid",
        "blocking_issues": [],
    }


def stage1_issue_summary(results: Sequence[Mapping[str, Any]]) -> JsonObject:
    """Count per-candidate judgments and overlapping reasons without sample text."""
    validated = [validate_stage1_result(dict(value)) for value in results]
    counts = Counter(issue for value in validated for issue in value["blocking_issues"])
    rejected = Counter(
        "/".join(value[key] for key in (
            "category_fit", "semantic_completeness", "intent_clarity", "validity"
        )) for value in validated if not stage1_passes(value)
    )
    return {
        "stage1_blocking_counts": {key: counts[key] for key in STAGE1_BLOCKING_ISSUES},
        "stage1_borderline": sum(value["validity"] == "borderline" for value in validated),
        "stage1_invalid": sum(value["validity"] == "invalid" for value in validated),
        "stage1_rejection_combinations": dict(sorted(rejected.items())),
    }


def stage2_semantic_review(results: Sequence[Mapping[str, Any]]) -> JsonObject:
    """Summarize final-member issues without implying human-gold accuracy."""
    validated = [validate_stage2_result(dict(value)) for value in results]
    counts = Counter(issue for value in validated for issue in value["execution_issues"])
    with_issues = sum(bool(value["execution_issues"]) for value in validated)
    return {
        "notice": "Stage2 语义问题复核信号；不是人工真值准确率。分类计数允许重叠。",
        "evaluated_final": len(validated),
        "with_issues": with_issues,
        "by_issue": {key: counts[key] for key in sorted(EXECUTION_ISSUES)},
        "rate": None if not validated else round(with_issues / len(validated), 6),
    }


def validate_challenge_result(value: Any) -> JsonObject:
    if not isinstance(value, dict) or set(value) != CHALLENGE_FIELDS:
        raise QualityResponseError("Challenge 响应字段必须与白名单完全一致")
    is_challenge = value.get("is_challenge")
    primary = value.get("primary_challenge")
    types = value.get("challenge_types")
    if type(is_challenge) is not bool:
        raise QualityResponseError("Challenge is_challenge 必须是布尔值")
    if not isinstance(types, list):
        raise QualityResponseError("Challenge challenge_types 必须是数组")
    if (
        any(not isinstance(item, str) or item not in CHALLENGE_TYPE_SET for item in types)
        or len(set(types)) != len(types)
        or len(types) > 3
    ):
        raise QualityResponseError("Challenge challenge_types 非法")
    if not is_challenge:
        if primary is not None or types:
            raise QualityResponseError("非 Challenge 必须使用 primary=null 且 types=[]")
        return {
            "is_challenge": False,
            "primary_challenge": None,
            "challenge_types": [],
        }
    if not types or primary not in CHALLENGE_TYPE_SET or primary not in types:
        raise QualityResponseError("Challenge primary 必须属于非空 challenge_types")
    ordered = [primary, *[item for item in CHALLENGE_TYPES if item in types and item != primary]]
    if types != ordered:
        raise QualityResponseError("Challenge types 必须以 primary 开头并按固定枚举顺序排列")
    return {
        "is_challenge": True,
        "primary_challenge": primary,
        "challenge_types": list(types),
    }


def challenge_passes(value: Mapping[str, Any]) -> bool:
    return bool(validate_challenge_result(dict(value))["is_challenge"])


def validate_stage2_result(value: Any) -> JsonObject:
    if not isinstance(value, dict) or set(value) != STAGE2_FIELDS:
        raise QualityResponseError("第二阶段响应字段必须与白名单完全一致")
    task = value.get("task_difficulty")
    intent_label = value.get("intent_label")
    execution_issues = value.get("execution_issues")
    if not isinstance(task, str) or task not in TASK_DIFFICULTIES:
        raise QualityResponseError("第二阶段 task_difficulty 枚举非法")
    if not isinstance(intent_label, str) or intent_label not in INTENT_LABELS:
        raise QualityResponseError("第二阶段 intent_label 非法")
    if (not isinstance(execution_issues, list)
            or any(not isinstance(item, str) or item not in EXECUTION_ISSUES for item in execution_issues)
            or len(set(execution_issues)) != len(execution_issues)):
        raise QualityResponseError("第二阶段 execution_issues 非法")
    if "no_evaluable_goal" in execution_issues and intent_label != "qa":
        raise QualityResponseError("无可评测目标时 intent_label 必须是 qa")
    no_goal = "no_evaluable_goal" in execution_issues
    if (task == "not_applicable") != no_goal:
        raise QualityResponseError("not_applicable 必须与 no_evaluable_goal 同时出现")
    if no_goal and execution_issues != ["no_evaluable_goal"]:
        raise QualityResponseError("no_evaluable_goal 不得与其他 execution_issues 并列")
    return {
        "task_difficulty": task,
        "intent_label": intent_label,
        "execution_issues": list(execution_issues),
    }


class TwoStageQualityRunner:
    """Concurrent three-stage runner with a run-local cache and sanitized ledgers."""

    def __init__(
        self,
        run_root: Path,
        *,
        stage1_prompt: str,
        challenge_prompt: str,
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
        if challenge_prompt.count("{CANDIDATE_JSON}") != 1:
            raise ValueError("Challenge Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
        self.run_root = run_root
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.prompts = {
            "stage1": stage1_prompt,
            "challenge": challenge_prompt,
            "stage2": stage2_prompt,
        }
        self.prompt_hashes = {key: _stable_hash(value) for key, value in self.prompts.items()}
        self.model = model
        self.llm_options = dict(llm_options)
        self.options_hash = _stable_hash({"model": model, "options": self.llm_options})
        self.llm_call = llm_call
        self.config = config or QualityRunConfig()
        self.logger = logger or logging.getLogger(__name__)
        self.cache_path = self.run_root / "llm_result_cache.sqlite"
        self._ledger_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._key_locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._request_gate = _FairRequestGate(self.config.workers)
        for stage, prompt in self.prompts.items():
            snapshot = self.run_root / f"{stage}_prompt.md"
            if snapshot.is_file() and snapshot.read_text(encoding="utf-8") != prompt:
                raise ValueError(f"{stage} Prompt 与本次运行已冻结快照不一致")
            if not snapshot.is_file():
                snapshot.write_text(prompt, encoding="utf-8", newline="\n")
        run_contract = {
            "quality_contract_version": QUALITY_CONTRACT_VERSION,
            "prompt_hashes": self.prompt_hashes,
            "model": self.model,
            "options_sha256": self.options_hash,
            "runner": asdict(self.config),
        }
        contract_path = self.run_root / "quality_run_contract.json"
        if contract_path.is_file():
            existing = json.loads(contract_path.read_text(encoding="utf-8"))
            if existing != run_contract:
                raise ValueError("质量 LLM 运行目录与当前 Prompt、模型或配置不兼容")
        else:
            contract_path.write_text(
                json.dumps(run_contract, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        self._initialize_cache()
        for stage in ("stage1", "challenge", "stage2"):
            cache_hit_path = self.run_root / f"{stage}_cache_hits.jsonl"
            if not cache_hit_path.exists():
                cache_hit_path.write_text("", encoding="utf-8", newline="\n")

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
                raise ValueError("质量 LLM 运行缓存完整性检查失败")

    def _cache_key(
        self,
        stage: str,
        candidate: Mapping[str, Any],
    ) -> str:
        if stage == "stage1":
            return stage1_task_key(
                candidate, prompt_sha256=self.prompt_hashes[stage],
                options_sha256=self.options_hash,
            )
        if stage == "stage2":
            return stage2_task_key(
                candidate, prompt_sha256=self.prompt_hashes[stage],
                options_sha256=self.options_hash,
            )
        taxonomy = candidate["taxonomy"]
        return _stable_hash(
            {
                "stage": stage,
                "prompt": self.prompt_hashes[stage],
                "options": self.options_hash,
                "conversation_sha256": candidate["conversation_sha256"],
                "l1": taxonomy["l1"],
                "l2": taxonomy["l2"],
                "stage1": None,
            }
        )

    def task_id(
        self,
        stage: str,
        candidate: Mapping[str, Any],
    ) -> str:
        """Return the frozen task/cache identity recorded in the quality ledgers."""
        if stage not in {"stage1", "challenge", "stage2"}:
            raise ValueError(f"未知质量阶段: {stage}")
        return self._cache_key(stage, candidate)

    def _load_cached(self, keys: Sequence[str], stage: str) -> dict[str, JsonObject]:
        if not keys:
            return {}
        result: dict[str, JsonObject] = {}
        with self._cache_lock:
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
        with self._cache_lock, sqlite3.connect(self.cache_path) as connection:
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
        branch: str,
    ) -> tuple[JsonObject, float, float, str]:
        payload = (
            stage1_quality_input(candidate) if stage == "stage1" else
            stage2_quality_input(candidate) if stage == "stage2" else quality_input(candidate)
        )
        prompt = self.prompts[stage].replace(
            "{CANDIDATE_JSON}", json.dumps(payload, ensure_ascii=False, indent=2)
        )
        if self.config.sleep_seconds:
            time.sleep(self.config.sleep_seconds)
        self._request_gate.acquire(branch)
        try:
            raw, first_latency, full_latency = self.llm_call(
                [{"role": "user", "content": prompt}],
                model=self.model,
                max_tokens=int(self.llm_options["max_tokens"]),
                stream=bool(self.llm_options["stream"]),
                enable_thinking=bool(self.llm_options["enable_thinking"]),
                skip_special_tokens=bool(self.llm_options["skip_special_tokens"]),
                timeout=float(self.llm_options["timeout"]),
            )
        finally:
            self._request_gate.release()
        success, parsed = try_parse_json(raw)
        if not success:
            raise QualityResponseError("响应不是合法 JSON 对象")
        if stage == "stage1":
            result = validate_stage1_result(parsed)
        elif stage == "challenge":
            result = validate_challenge_result(parsed)
        else:
            result = validate_stage2_result(parsed)
        return result, first_latency, full_latency, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _call_singleflight(
        self,
        key: str,
        stage: str,
        candidate: Mapping[str, Any],
        branch: str,
    ) -> tuple[JsonObject, float, float, str, bool]:
        """Reuse a concurrent branch's result instead of issuing the same request twice."""
        with self._key_locks_guard:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            concurrent_hit = self._load_cached([key], stage).get(key)
            if concurrent_hit is not None:
                output_hash = _stable_hash(concurrent_hit)
                return concurrent_hit, 0.0, 0.0, output_hash, True
            result, first_latency, full_latency, output_hash = self._call(
                stage, candidate, branch
            )
            # Commit while holding the key lock so every waiter observes the result.
            self._save_cached(stage, {key: result})
            return result, first_latency, full_latency, output_hash, False

    def _run(
        self,
        stage: str,
        candidates: Sequence[JsonObject],
        *,
        branch: str = "shared",
    ) -> dict[str, JsonObject]:
        by_key: dict[str, list[JsonObject]] = defaultdict(list)
        for candidate in candidates:
            key = self._cache_key(stage, candidate)
            by_key[key].append(candidate)
        cached = self._load_cached(list(by_key), stage)
        for value in cached.values():
            if stage == "stage1":
                validate_stage1_result(value)
            elif stage == "challenge":
                validate_challenge_result(value)
            else:
                validate_stage2_result(value)
        if cached:
            cache_hit_rows = []
            for key in sorted(cached):
                identities = sorted({
                    str(candidate["source_identity_sha256"])
                    for candidate in by_key[key]
                })
                cache_hit_rows.append({
                    "event_id": _stable_hash({
                        "stage": stage,
                        "cache_key": key,
                        "source_identities": identities,
                    }),
                    "cache_key": key,
                    "stage": stage,
                    "source_identities": identities,
                })
            with self._ledger_lock:
                _merge_ledger(
                    self.run_root / f"{stage}_cache_hits.jsonl",
                    cache_hit_rows,
                    "event_id",
                )

        tasks = []
        for key, grouped in sorted(by_key.items()):
            representative = grouped[0]
            tasks.append(
                {
                    "cache_key": key,
                    "stage": stage,
                    "source_lines": sorted({int(item["source"]["source_line"]) for item in grouped}),
                    "source_identities": sorted({item["source_identity_sha256"] for item in grouped}),
                    "conversation_sha256": representative["conversation_sha256"],
                    **(
                        {"input_sha256": stage2_input_sha256(representative)}
                        if stage == "stage2"
                        else {
                            "input_sha256": stage1_input_sha256(representative),
                            "candidate_category_sha256": _stable_hash(
                                stage1_quality_input(representative)["candidate_category"]
                            ),
                        } if stage == "stage1"
                        else {"taxonomy_sha256": _stable_hash(representative["taxonomy"])}
                    ),
                    "prompt_sha256": self.prompt_hashes[stage],
                    "options_sha256": self.options_hash,
                }
            )
        with self._ledger_lock:
            _merge_task_ledger(self.run_root / f"{stage}_tasks.jsonl", tasks)

        pending = [key for key in by_key if key not in cached]
        self.logger.info(
            "%s tasks=%d cache_hits=%d pending=%d",
            stage,
            len(by_key),
            len(cached),
            len(pending),
        )
        failures: dict[str, str] = {}
        attempt_path = self.run_root / f"{stage}_attempts.jsonl"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt_no in range(1, self.config.max_retries + 1):
            if not pending:
                break
            self.logger.info(
                "%s attempt=%d pending=%d workers=%d",
                stage,
                attempt_no,
                len(pending),
                self.config.workers,
            )
            failures = {}
            successes: dict[str, JsonObject] = {}
            concurrent_hits: list[str] = []
            with ThreadPoolExecutor(max_workers=self.config.workers) as executor:
                futures = {
                    executor.submit(
                        self._call_singleflight,
                        key,
                        stage,
                        by_key[key][0],
                        branch,
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
                        result, first_latency, full_latency, output_hash, was_cached = future.result()
                        successes[key] = result
                        if was_cached:
                            concurrent_hits.append(key)
                        attempt.update(
                            {
                                "outcome": "success",
                                "output_sha256": output_hash,
                                "first_token_latency_ms": first_latency,
                                "full_latency_ms": full_latency,
                                "cache_hit": was_cached,
                                "error": None,
                            }
                        )
                    except Exception as exc:  # transport and contract failures are both retried
                        failures[key] = _sanitized_error(exc)
                        attempt.update(
                            {
                                "outcome": "failure",
                                "output_sha256": None,
                                "first_token_latency_ms": None,
                                "full_latency_ms": None,
                                "cache_hit": False,
                                "error": failures[key],
                            }
                        )
                    with self._ledger_lock, attempt_path.open(
                        "a", encoding="utf-8", newline="\n"
                    ) as attempt_file:
                        attempt_file.write(json.dumps(attempt, ensure_ascii=False) + "\n")
            if successes:
                # _call_singleflight already committed provider results. This update
                # only merges both provider results and concurrent cache hits locally.
                cached.update(successes)
            if concurrent_hits:
                rows = [
                    {
                        "event_id": _stable_hash({
                            "stage": stage,
                            "cache_key": key,
                            "source_identities": sorted({
                                str(candidate["source_identity_sha256"])
                                for candidate in by_key[key]
                            }),
                        }),
                        "cache_key": key,
                        "stage": stage,
                        "source_identities": sorted({
                            str(candidate["source_identity_sha256"])
                            for candidate in by_key[key]
                        }),
                    }
                    for key in concurrent_hits
                ]
                with self._ledger_lock:
                    _merge_ledger(
                        self.run_root / f"{stage}_cache_hits.jsonl", rows, "event_id"
                    )
            pending = [key for key in pending if key not in successes]
            self.logger.info(
                "%s attempt=%d succeeded=%d failed=%d remaining=%d",
                stage,
                attempt_no,
                len(successes),
                len(failures),
                len(pending),
            )
            if pending and attempt_no < self.config.max_retries:
                time.sleep(min(2**attempt_no, 10))
        if pending:
            raise QualityStageIncomplete(stage, [f"{key}:{failures.get(key, 'unknown')}" for key in pending])

        cache_results = [
            {"cache_key": key, "stage": stage, "result": cached[key]}
            for key in sorted(by_key)
        ]
        with self._ledger_lock:
            _merge_ledger(self.run_root / f"{stage}_results.jsonl", cache_results, "cache_key")
        result_by_identity: dict[str, JsonObject] = {}
        for key, grouped in by_key.items():
            for candidate in grouped:
                result_by_identity[candidate["source_identity_sha256"]] = dict(cached[key])
        return result_by_identity

    def run_stage1(
        self, candidates: Sequence[JsonObject], *, branch: str = "shared"
    ) -> dict[str, JsonObject]:
        return self._run("stage1", candidates, branch=branch)

    def run_challenge(
        self, candidates: Sequence[JsonObject], *, branch: str = "shared"
    ) -> dict[str, JsonObject]:
        return self._run("challenge", candidates, branch=branch)

    def run_stage2(
        self,
        candidates: Sequence[JsonObject],
        *,
        branch: str = "shared",
    ) -> dict[str, JsonObject]:
        return self._run("stage2", candidates, branch=branch)


def run_stage1(
    candidate: JsonObject,
    *,
    runner: TwoStageQualityRunner,
) -> JsonObject:
    """Annotate one candidate with the first-stage contract."""
    return runner.run_stage1([candidate])[candidate["source_identity_sha256"]]


def run_challenge(
    candidate: JsonObject,
    *,
    runner: TwoStageQualityRunner,
) -> JsonObject:
    """Annotate one Stage1-admitted candidate with the Challenge contract."""
    return runner.run_challenge([candidate])[candidate["source_identity_sha256"]]


def run_stage2(
    candidate: JsonObject,
    *,
    runner: TwoStageQualityRunner,
) -> JsonObject:
    """Annotate one admitted candidate with the second-stage contract."""
    identity = candidate["source_identity_sha256"]
    return runner.run_stage2([candidate])[identity]
