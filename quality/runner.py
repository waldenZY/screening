"""Independent feature/category requests, deterministic admission and downstream annotation."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..shared.json_io import JsonObject, load_jsonl, try_parse_json, write_jsonl
from ..shared.challenge import CHALLENGE_TYPES
from ..shared.llm import LlmCall
from ..candidate.contract import parse_conversation
from .admission import (stable_hash as _stable_hash)
from .admission import (DEFAULT_POLICY, QualityResponseError, validate_features, validate_category, validate_category_response, validate_policy, build_admission_result, validate_admission_result)

CHALLENGE_FIELDS = frozenset(
    {"is_challenge", "primary_challenge", "challenge_types"}
)
CHALLENGE_TYPE_SET = frozenset(CHALLENGE_TYPES)
QUALITY_CONTRACT_VERSION = 14


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


def challenge_quality_input(candidate: Mapping[str, Any], features: Mapping[str, Any]) -> JsonObject:
    conversation = candidate["conversation"]
    taxonomy = candidate["taxonomy"]
    return {
        "sample_type": conversation["sample_type"],
        "context_turns": list(conversation["context_turns"]),
        "target_query": conversation["target_query"],
        "L1": taxonomy["l1"],
        "L2": taxonomy["l2"],
        "admission_features": dict(features),
    }


def conversation_quality_input(candidate: Mapping[str, Any]) -> JsonObject:
    """Describe the whole ordered sample without labels or upstream judgments."""
    conversation = candidate["conversation"]
    parsed = parse_conversation(conversation["full_text"])
    if conversation["sample_type"] != parsed["sample_type"]:
        raise ValueError("sample_type 与完整会话轮次不一致")
    return {
        "sample_type": parsed["sample_type"],
        "conversation_turns": parsed["full_text"].split(" | "),
    }


def admission_quality_input(candidate: Mapping[str, Any]) -> JsonObject:
    """Preserve every turn; isolate the candidate category used only for checking."""
    payload = conversation_quality_input(candidate)
    taxonomy = candidate["taxonomy"]
    return {
        **payload,
        "candidate_category": {"L1": taxonomy["l1"], "L2": taxonomy["l2"]},
    }


def admission_input_sha256(candidate: Mapping[str, Any]) -> str:
    return _stable_hash(admission_quality_input(candidate))


def admission_task_key(
    candidate: Mapping[str, Any], *, prompt_sha256: str, options_sha256: str,
) -> str:
    return _stable_hash({
        "stage": "admission", "prompt": prompt_sha256, "options": options_sha256,
        "input_sha256": admission_input_sha256(candidate),
    })


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


@dataclass(frozen=True)
class AdditionalQualityStage:
    """A supplementary annotation with its own input and response contract."""
    prompt: str
    input_builder: Callable[..., JsonObject]
    validator: Callable[[Any], JsonObject]


class QualityRunner:
    """Concurrent isolated model stages with a run-local cache and sanitized ledgers."""

    def __init__(
        self,
        run_root: Path,
        *,
        admission_prompt: str,
        category_prompt: str | None = None,
        screening_policy: Mapping[str, Any] | None = None,
        challenge_prompt: str,
        model: str,
        llm_options: Mapping[str, Any],
        llm_call: LlmCall,
        config: QualityRunConfig | None = None,
        logger: logging.Logger | None = None,
        additional_stages: Mapping[str, AdditionalQualityStage] | None = None,
    ) -> None:
        if admission_prompt.count("{CANDIDATE_JSON}") != 1:
            raise ValueError("准入文本特征 Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
        if challenge_prompt.count("{CANDIDATE_JSON}") != 1:
            raise ValueError("Challenge Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
        self.run_root = run_root
        self.run_root.mkdir(parents=True, exist_ok=True)
        if category_prompt is None:
            category_prompt = (Path(__file__).resolve().parents[1] / "artifacts/prompts/quality_admission_category.md").read_text(encoding="utf-8")
        if category_prompt.count("{CANDIDATE_JSON}") != 1:
            raise ValueError("类别 Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
        self.screening_policy = validate_policy(dict(DEFAULT_POLICY if screening_policy is None else screening_policy))
        self._admission_by_identity: dict[str, JsonObject] = {}
        self.prompts = {
            "admission_features": admission_prompt,
            "admission_category": category_prompt,
            "challenge": challenge_prompt,
        }
        from .annotation import stage_definition
        if additional_stages is None:
            prompt = (Path(__file__).resolve().parents[1] / "artifacts/prompts/quality_annotation.md").read_text(encoding="utf-8")
            additional_stages = {"quality_annotation": stage_definition(prompt)}
        if set(additional_stages) != {"quality_annotation"}:
            raise ValueError("完整链路必须配置下游质量标注")
        self.additional_stages = dict(additional_stages)
        for name, definition in self.additional_stages.items():
            if (not name.isidentifier() or not name.isascii() or name in {*self.prompts, "admission"}
                    or definition.prompt.count("{CANDIDATE_JSON}") != 1):
                raise ValueError("附加标注阶段名称或 Prompt 非法")
            self.prompts[name] = definition.prompt
        self.prompt_hashes = {key: _stable_hash(value) for key, value in self.prompts.items()}
        self.prompt_hashes["admission"] = _stable_hash({
            "features": self.prompt_hashes["admission_features"],
            "category": self.prompt_hashes["admission_category"],
            "policy": self.screening_policy,
        })
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
            "screening_policy": self.screening_policy,
            "additional_stages": sorted(self.additional_stages),
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
        for stage in ("admission", *self.prompts):
            for suffix in ("tasks", "results", "attempts"):
                path = self.run_root / f"{stage}_{suffix}.jsonl"
                if not path.exists():
                    path.write_text("", encoding="utf-8")
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
        if stage == "admission":
            return admission_task_key(
                candidate, prompt_sha256=self.prompt_hashes[stage],
                options_sha256=self.options_hash,
            )
        if stage in {"admission_features", "admission_category"}:
            payload = conversation_quality_input(candidate) if stage == "admission_features" else admission_quality_input(candidate)
            return _stable_hash({"stage": stage, "prompt": self.prompt_hashes[stage], "options": self.options_hash, "input_sha256": _stable_hash(payload)})
        if stage in self.additional_stages:
            return _stable_hash({"stage": stage, "prompt": self.prompt_hashes[stage],
                                 "options": self.options_hash, "input_sha256": _stable_hash(self._payload(stage, candidate))})
        taxonomy = candidate["taxonomy"]
        return _stable_hash(
            {
                "stage": stage,
                "prompt": self.prompt_hashes[stage],
                "options": self.options_hash,
                "conversation_sha256": candidate["conversation_sha256"],
                "l1": taxonomy["l1"],
                "l2": taxonomy["l2"],
                "admission_features": self._admission_by_identity[candidate["source_identity_sha256"]]["admission"]["features"],
            }
        )

    def task_id(
        self,
        stage: str,
        candidate: Mapping[str, Any],
    ) -> str:
        """Return the frozen task/cache identity recorded in the quality ledgers."""
        if stage not in {"admission", *self.prompts}:
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
        payload = self._payload(stage, candidate)
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
        result = self._validate_response(stage, parsed, candidate)
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
        for key, value in cached.items():
            self._validate_response(stage, value, by_key[key][0], cached=True)
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
                    "input_sha256": _stable_hash(self._payload(stage, representative)),
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

    def _payload(self, stage: str, candidate: Mapping[str, Any]) -> JsonObject:
        if stage == "quality_annotation":
            return self.additional_stages[stage].input_builder(
                candidate, self._admission_by_identity[candidate["source_identity_sha256"]]
            )
        if stage in self.additional_stages:
            return self.additional_stages[stage].input_builder(candidate)
        if stage == "admission_features":
            return conversation_quality_input(candidate)
        if stage == "admission_category":
            return admission_quality_input(candidate)
        return challenge_quality_input(candidate, self._admission_by_identity[candidate["source_identity_sha256"]]["admission"]["features"])

    def _validate_response(self, stage: str, value: Any, candidate: Mapping[str, Any], *, cached: bool = False) -> JsonObject:
        if stage == "admission_features":
            return validate_features(value)
        if stage == "admission_category":
            return validate_category(value) if cached else validate_category_response(value)
        if stage == "challenge":
            return validate_challenge_result(value)
        if stage in self.additional_stages:
            return self.additional_stages[stage].validator(value)
        raise ValueError(f"未知质量阶段: {stage}")

    def bind_admission(self, results: Mapping[str, JsonObject]) -> None:
        for identity, value in results.items():
            self._admission_by_identity[identity] = validate_admission_result(value)

    def run_admission(
        self, candidates: Sequence[JsonObject], *, branch: str = "shared"
    ) -> dict[str, JsonObject]:
        features = self._run("admission_features", candidates, branch=branch)
        categories = self._run("admission_category", candidates, branch=branch)
        results, tasks, rows = {}, [], []
        for candidate in candidates:
            identity = candidate["source_identity_sha256"]
            result = build_admission_result(features[identity], categories[identity], self.screening_policy)
            key = self.task_id("admission", candidate)
            tasks.append({
                "cache_key": key, "stage": "admission", "source_lines": [int(candidate["source"]["source_line"])],
                "source_identities": [identity], "conversation_sha256": candidate["conversation_sha256"],
                "input_sha256": admission_input_sha256(candidate),
                "candidate_category_sha256": _stable_hash(admission_quality_input(candidate)["candidate_category"]),
                "prompt_sha256": self.prompt_hashes["admission"], "options_sha256": self.options_hash,
                "feature_task_id": self.task_id("admission_features", candidate),
                "category_task_id": self.task_id("admission_category", candidate),
                "policy_sha256": _stable_hash(self.screening_policy),
            })
            rows.append({"cache_key": key, "stage": "admission", "result": result})
            results[identity] = result
        with self._ledger_lock:
            _merge_task_ledger(self.run_root / "admission_tasks.jsonl", tasks)
            _merge_ledger(self.run_root / "admission_results.jsonl", rows, "cache_key")
        self.bind_admission(results)
        return results

    def run_challenge(
        self, candidates: Sequence[JsonObject], admission: Mapping[str, JsonObject] | None = None, *, branch: str = "shared"
    ) -> dict[str, JsonObject]:
        if admission is not None:
            self.bind_admission(admission)
        return self._run("challenge", candidates, branch=branch)

    def run_additional_stage(
        self, stage: str, candidates: Sequence[JsonObject], *, branch: str = "shared",
    ) -> dict[str, JsonObject]:
        if stage not in self.additional_stages:
            raise ValueError(f"附加标注阶段未启用: {stage}")
        return self._run(stage, candidates, branch=branch)
