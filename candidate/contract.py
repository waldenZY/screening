"""Versioned canonical candidate contract and deterministic fixed features."""

from __future__ import annotations

import hashlib
import bisect
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..shared.json_io import JsonObject
from ..shared.taxonomy import TaxonomyMappings


CANONICAL_SCHEMA_VERSION = 1
FEATURE_RULE_VERSION = 2
MAX_SQLITE_INTEGER = 2**63 - 1

PROBABILITY_BANDS = frozenset({"high", "mid", "boundary"})
FREQUENCY_BANDS = frozenset({"hot", "warm", "cold"})
LENGTH_BANDS = frozenset({"short", "mid", "long"})
COMPLEXITY_LEVELS = frozenset({"simple", "moderate", "complex"})
FORMALITY_LEVELS = frozenset({"formal", "mixed", "colloquial"})
SAMPLE_TYPES = frozenset({"single_turn", "multi_turn"})

TRACE_FIELDS = (
    "sn",
    "raw_index",
    "seg_id",
    "turn_range",
    "raw_query",
    "original_turn_texts",
    "boundary_confidence",
    "topic_hint",
)

# These are complete legacy markers.  The old builder accidentally used character
# classes, causing ordinary characters such as A/S/R to be removed or matched.
MULTIMODAL_MARKER_PATTERN = (
    r"\[\(\(多模态\)输入\)\]|\[\(多模态\)\]|\[ASR\]|\[\(图片\)\]|\[\(语音\)\]"
)
_MULTIMODAL_MARKERS = re.compile(MULTIMODAL_MARKER_PATTERN)
_LEADING_MULTIMODAL_MARKER = re.compile(rf"^(?:{MULTIMODAL_MARKER_PATTERN})")
_WHITESPACE = re.compile(r"\s+")

_PARTICLES = set("呢嘛吧啊哦哈啦呗呀哇咯撒咔咚嘞喽咧")
_COLLOQUIAL_CHARS = set("啥咋俺咱整搁")
_CASUAL_WORDS = re.compile(
    r"(咋整|干哈|干啥|整啥|咋么|俺们|啥子|咋办|啥意思|整一下|咋样"
    r"|麻了|离谱|裂开|烦死了|无语|救命|服了|没谁了|我靠|卧槽|尼玛)"
)
_BADWORDS = re.compile(r"(他妈的|他妈|操你|傻逼|煞笔|脑残|垃圾|废物|狗屁|cnm|rnm|tm的|sb)")
_ACTIONS = re.compile(
    r"打开|关闭|取消|设置|删除|修改|查询|播放|停止|暂停|重启|关机|发送|拨号|"
    r"导航|翻译|截图|搜索|计算|定位|预约|订|买"
)
_CONNECTORS = re.compile(r"并且|同时|但是|另外|而且|或者|还是|然后|还有|因为|所以|如果|虽然")


class CanonicalContractError(ValueError):
    """A contract error carrying a stable, non-sensitive issue code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FixedFeatureConfig:
    probability_high: float = 0.9
    probability_mid: float = 0.5
    frequency_hot_percentile: float = 0.8
    frequency_warm_percentile: float = 0.2
    p0_probability_weight: float = 0.6
    p0_frequency_weight: float = 0.4
    p2_probability_weight: float = 0.5
    p2_frequency_weight: float = 0.5

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= float(value) <= 1.0
            for value in values.values()
        ):
            raise ValueError("固定特征阈值和权重必须是 [0, 1] 内的有限数值")
        if self.probability_mid > self.probability_high:
            raise ValueError("probability_mid 不能大于 probability_high")
        if self.frequency_warm_percentile > self.frequency_hot_percentile:
            raise ValueError("frequency_warm_percentile 不能大于 frequency_hot_percentile")
        for label, total in (
            ("P0", self.p0_probability_weight + self.p0_frequency_weight),
            ("P2", self.p2_probability_weight + self.p2_frequency_weight),
        ):
            if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"{label} relevance 权重之和必须为 1")

    def as_dict(self) -> JsonObject:
        return asdict(self)


DEFAULT_FIXED_FEATURE_CONFIG = FixedFeatureConfig()


def _stable_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_turn(value: str) -> str:
    normalized = _WHITESPACE.sub(" ", value).strip()
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalContractError("invalid_text_encoding", "text 包含非法 Unicode") from exc
    return normalized


def clean_text(value: str) -> str:
    """Apply the legacy text cleaning intent with exact marker matching."""
    normalized = _normalize_turn(value)
    return _MULTIMODAL_MARKERS.sub("", normalized).strip()


def parse_conversation(text: Any) -> JsonObject:
    """Parse cluster ``text`` using the canonical conversation semantics."""
    if not isinstance(text, str):
        raise CanonicalContractError("invalid_text_type", "cluster text 必须是字符串")
    turns = [_normalize_turn(part) for part in text.split("|")]
    turns = [turn for turn in turns if turn]
    if not turns:
        raise CanonicalContractError("empty_text", "cluster text 规范化后为空")
    target_query = _MULTIMODAL_MARKERS.sub("", turns[-1]).strip()
    if not target_query:
        raise CanonicalContractError("empty_target_query", "目标轮移除标记后为空")
    context_turns = turns[:-1]
    return {
        "full_text": " | ".join(turns),
        "context_turns": context_turns,
        "target_query": target_query,
        "turn_count": len(turns),
        "sample_type": "multi_turn" if len(turns) >= 2 else "single_turn",
    }


def source_identity_sha256(session_id: str, raw_text: str) -> str:
    """Hash normalized session plus the original cluster text."""
    if not isinstance(session_id, str):
        raise CanonicalContractError("missing_session", "session 必须是字符串")
    normalized_session = session_id.strip()
    if not normalized_session:
        raise CanonicalContractError("missing_session", "session 不能为空")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise CanonicalContractError("missing_text", "text 不能为空")
    try:
        return _stable_sha256([normalized_session, raw_text])
    except UnicodeEncodeError as exc:
        raise CanonicalContractError(
            "invalid_identity_encoding",
            "session 或 text 包含非法 Unicode",
        ) from exc


def conversation_sha256(conversation: Mapping[str, Any]) -> str:
    context = conversation.get("context_turns")
    target = conversation.get("target_query")
    if not isinstance(context, list) or not all(isinstance(item, str) for item in context):
        raise CanonicalContractError("invalid_context_turns", "context_turns 必须是字符串数组")
    if not isinstance(target, str) or not target:
        raise CanonicalContractError("empty_target_query", "target_query 不能为空")
    try:
        return _stable_sha256({"context_turns": context, "target_query": target})
    except UnicodeEncodeError as exc:
        raise CanonicalContractError(
            "invalid_conversation_encoding",
            "会话包含非法 Unicode",
        ) from exc


def normalize_source_signals(source_row: Mapping[str, Any]) -> tuple[JsonObject, tuple[str, ...]]:
    """Validate source signals and return defaults used as stable issue codes."""
    defaulted: list[str] = []
    if "cluster_prob" not in source_row or source_row.get("cluster_prob") is None:
        probability = 0.0
        defaulted.append("missing_cluster_probability_defaulted")
    else:
        value = source_row.get("cluster_prob")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CanonicalContractError(
                "invalid_cluster_probability_type",
                "cluster_probability 必须是有限数值",
            )
        probability = float(value)
        if not 0.0 <= probability <= 1.0:
            raise CanonicalContractError(
                "cluster_probability_out_of_range",
                "cluster_probability 必须位于 [0, 1]",
            )

    if "frequency" not in source_row or source_row.get("frequency") is None:
        frequency = 1
        defaulted.append("missing_frequency_defaulted")
    else:
        value = source_row.get("frequency")
        if isinstance(value, bool) or not isinstance(value, int):
            raise CanonicalContractError(
                "invalid_frequency_type",
                "frequency 必须是非负整数",
            )
        if value < 0:
            raise CanonicalContractError("negative_frequency", "frequency 不能为负数")
        if value > MAX_SQLITE_INTEGER:
            raise CanonicalContractError(
                "frequency_out_of_range",
                "frequency 超出索引整数范围",
            )
        frequency = value
    return {"cluster_probability": probability, "frequency": frequency}, tuple(defaulted)


def normalize_trace_fields(source_row: Mapping[str, Any]) -> JsonObject:
    """Return the trace whitelist and reject invalid present values."""
    trace = {name: source_row.get(name) for name in TRACE_FIELDS}
    trace_types: dict[str, tuple[type, ...]] = {
        "sn": (str,),
        "raw_index": (int,),
        "seg_id": (int,),
        "turn_range": (list,),
        "raw_query": (str,),
        "original_turn_texts": (str, list),
        "boundary_confidence": (str,),
        "topic_hint": (str,),
    }
    for key, allowed in trace_types.items():
        value = trace[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, allowed)):
            raise CanonicalContractError(
                f"invalid_trace_{key}",
                f"追溯字段 {key} 类型非法",
            )
        string_values = value if isinstance(value, list) else [value]
        for item in string_values:
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise CanonicalContractError(
                        f"invalid_trace_{key}",
                        f"追溯字段 {key} 包含非法 Unicode",
                    ) from exc
    turn_range = trace["turn_range"]
    if turn_range is not None and (
        len(turn_range) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in turn_range)
    ):
        raise CanonicalContractError("invalid_trace_turn_range", "追溯字段 turn_range 非法")
    original_turns = trace["original_turn_texts"]
    if isinstance(original_turns, list) and not all(isinstance(item, str) for item in original_turns):
        raise CanonicalContractError(
            "invalid_trace_original_turn_texts",
            "追溯字段 original_turn_texts 非法",
        )
    return trace


def _length_band(text: str) -> str:
    length = len(clean_text(text))
    if length < 10:
        return "short"
    if length <= 30:
        return "mid"
    return "long"


def _complexity(text: str) -> str:
    score = text.count("|") * 0.3
    if _MULTIMODAL_MARKERS.search(text):
        score += 0.5
    if re.search(r"[?？]", text):
        score += 0.3
    cleaned = clean_text(text)
    if re.search(r"\b\d{3,}\b", cleaned) and not re.search(
        r"(计时|倒计时|定时)\d{1,3}(分钟|秒|小时)", cleaned
    ):
        score += 0.3
    if len(cleaned) > 50:
        score += 0.7
    elif len(cleaned) > 30:
        score += 0.4
    elif len(cleaned) > 20:
        score += 0.2
    if _CONNECTORS.search(cleaned):
        score += 0.3
    clean_turns = [
        turn.strip()
        for turn in text.split("|")
        if turn.strip()
        and len(turn.strip()) > 2
        and not _LEADING_MULTIMODAL_MARKER.match(turn.strip())
    ]
    if len(clean_turns) >= 4:
        score += 0.8
    elif len(clean_turns) >= 3:
        score += 0.5
    elif len(clean_turns) >= 2:
        score += 0.3
    action_count = len(_ACTIONS.findall(cleaned))
    if action_count >= 3:
        score += 1.0
    elif action_count >= 2:
        score += 0.5
    if score <= 0.3:
        return "simple"
    if score <= 1.5:
        return "moderate"
    return "complex"


def _formality(text: str) -> str:
    cleaned = clean_text(text)
    score = sum(1 for char in cleaned if char in _PARTICLES) * 0.25
    score += sum(1 for char in cleaned if char in _COLLOQUIAL_CHARS) * 0.4
    score += len(_CASUAL_WORDS.findall(cleaned)) * 0.6
    if _BADWORDS.search(cleaned.lower()):
        score += 1.0
    if re.search(r"~~{1,}", cleaned):
        score += 0.5
    if re.search(r"！{2,}", cleaned):
        score += 0.3
    # Kept for output compatibility: this is the historical rule and threshold.
    if re.search(r".{3,}", cleaned):
        score += 0.3
    if re.search(r"(.)\1{2,}", cleaned):
        score += 0.5
    if re.search(r"[\U0001F300-\U0001F9FF]|[;:][-~^]?[)D(p]", text):
        score += 0.3
    if score <= 0.5:
        return "formal"
    if score <= 1.5:
        return "mixed"
    return "colloquial"


def compute_fixed_features(
    signals: Mapping[str, Any],
    conversation: Mapping[str, Any],
    cluster_frequency_stats: Mapping[str, Any],
    config: FixedFeatureConfig | Mapping[str, Any] | None = None,
) -> JsonObject:
    """Compute deterministic legacy-compatible features without rounding."""
    if config is None:
        rules = DEFAULT_FIXED_FEATURE_CONFIG
    elif isinstance(config, FixedFeatureConfig):
        rules = config
    else:
        rules = FixedFeatureConfig(**dict(config))

    probability = signals.get("cluster_probability")
    frequency = signals.get("frequency")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise CanonicalContractError("invalid_cluster_probability_type", "cluster_probability 非法")
    if not math.isfinite(probability) or not 0.0 <= float(probability) <= 1.0:
        raise CanonicalContractError("cluster_probability_out_of_range", "cluster_probability 非法")
    if (
        isinstance(frequency, bool)
        or not isinstance(frequency, int)
        or not 0 <= frequency <= MAX_SQLITE_INTEGER
    ):
        raise CanonicalContractError("invalid_frequency", "frequency 非法")

    total_count = cluster_frequency_stats.get("total_count")
    less_count = cluster_frequency_stats.get("less_count")
    min_frequency = cluster_frequency_stats.get("min_frequency")
    max_frequency = cluster_frequency_stats.get("max_frequency")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count <= 0
        or isinstance(less_count, bool)
        or not isinstance(less_count, int)
        or not 0 <= less_count < total_count
        or isinstance(min_frequency, bool)
        or not isinstance(min_frequency, int)
        or isinstance(max_frequency, bool)
        or not isinstance(max_frequency, int)
        or min_frequency < 0
        or max_frequency < min_frequency
        or not min_frequency <= frequency <= max_frequency
    ):
        raise CanonicalContractError("invalid_cluster_frequency_stats", "cluster frequency 统计非法")

    percentile = less_count / total_count
    if percentile >= rules.frequency_hot_percentile:
        frequency_band = "hot"
    elif percentile >= rules.frequency_warm_percentile:
        frequency_band = "warm"
    else:
        frequency_band = "cold"

    min_log = math.log1p(min_frequency)
    max_log = math.log1p(max_frequency)
    normalized_frequency = (
        1.0
        if max_log == min_log
        else (math.log1p(frequency) - min_log) / (max_log - min_log)
    )
    if probability >= rules.probability_high:
        probability_band = "high"
    elif probability >= rules.probability_mid:
        probability_band = "mid"
    else:
        probability_band = "boundary"

    full_text = conversation.get("full_text")
    if not isinstance(full_text, str) or not full_text:
        raise CanonicalContractError("invalid_full_text", "full_text 不能为空")
    return {
        "probability_band": probability_band,
        "frequency_band": frequency_band,
        "normalized_frequency": normalized_frequency,
        "relevance": {
            "p0": (
                rules.p0_probability_weight * float(probability)
                + rules.p0_frequency_weight * normalized_frequency
            ),
            "p2": (
                rules.p2_probability_weight * (1.0 - float(probability))
                + rules.p2_frequency_weight * (1.0 - normalized_frequency)
            ),
        },
        "length_band": _length_band(full_text),
        "complexity": _complexity(full_text),
        "formality": _formality(full_text),
    }


def _taxonomy_values(cluster_id: Any, taxonomy: TaxonomyMappings) -> tuple[str, str, str]:
    cluster_kind = (
        "integer"
        if type(cluster_id) is int
        else "string"
        if isinstance(cluster_id, str) and cluster_id and cluster_id == cluster_id.strip()
        else None
    )
    if cluster_kind != taxonomy.cluster_id_type:
        raise CanonicalContractError(
            "cluster_id_type_mismatch",
            "cluster ID 类型与 taxonomy 不一致",
        )
    l1 = taxonomy.cluster_to_l1.get(cluster_id)
    l2_key = taxonomy.cluster_to_l2.get(cluster_id)
    if not isinstance(l1, str) or not l1:
        raise CanonicalContractError("missing_l1_mapping", "cluster 缺少 L1 映射")
    if not isinstance(l2_key, str) or not l2_key.startswith(f"{l1}/"):
        raise CanonicalContractError("missing_l2_mapping", "cluster 缺少 L2 映射")
    l2 = l2_key[len(l1) + 1 :]
    if not l2:
        raise CanonicalContractError("missing_l2_mapping", "cluster 缺少 L2 映射")
    return l1, l2, l2_key


def build_canonical_candidate(
    source_row: Mapping[str, Any],
    locator: Mapping[str, Any],
    taxonomy: TaxonomyMappings,
    indexed_features: Mapping[str, Any],
) -> JsonObject:
    """Materialize one canonical record from an indexed source row."""
    raw_text = source_row.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise CanonicalContractError("missing_text", "cluster text 不能为空")
    raw_session = source_row.get("session")
    if not isinstance(raw_session, str) or not raw_session.strip():
        raise CanonicalContractError("missing_session", "cluster session 不能为空")
    session_id = raw_session.strip()
    cluster_id = source_row.get("cluster")
    l1, l2, l2_key = _taxonomy_values(cluster_id, taxonomy)
    conversation = parse_conversation(raw_text)
    signals, _ = normalize_source_signals(source_row)
    source_identity = source_identity_sha256(session_id, raw_text)
    conversation_hash = conversation_sha256(conversation)

    expected_pairs = (
        ("source_identity_sha256", source_identity),
        ("conversation_sha256", conversation_hash),
        ("cluster_id", cluster_id),
        ("l1", l1),
        ("l2", l2),
        ("l2_key", l2_key),
        ("sample_type", conversation["sample_type"]),
        ("cluster_probability", signals["cluster_probability"]),
        ("frequency", signals["frequency"]),
    )
    for name, actual in expected_pairs:
        if name in indexed_features and indexed_features[name] != actual:
            raise CanonicalContractError(
                "indexed_value_mismatch",
                f"索引字段 {name} 与源记录不一致",
            )

    features = indexed_features.get("features")
    if not isinstance(features, dict):
        feature_keys = {
            "probability_band",
            "frequency_band",
            "normalized_frequency",
            "relevance",
            "length_band",
            "complexity",
            "formality",
        }
        features = {key: indexed_features[key] for key in feature_keys if key in indexed_features}

    trace = normalize_trace_fields(source_row)
    record: JsonObject = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "source_identity_sha256": source_identity,
        "conversation_sha256": conversation_hash,
        "source": {
            "source_snapshot_id": locator.get("source_snapshot_id"),
            "source_line": locator.get("source_line"),
            "byte_offset": locator.get("byte_offset"),
            "byte_length": locator.get("byte_length"),
            "record_sha256": locator.get("record_sha256"),
            "session_id": session_id,
            "raw_text": raw_text,
            "trace": trace,
        },
        "conversation": conversation,
        "taxonomy": {
            "cluster_id": cluster_id,
            "l1": l1,
            "l2": l2,
            "l2_key": l2_key,
        },
        "signals": signals,
        "features": features,
    }
    validate_canonical_candidate(record)
    return record


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CanonicalContractError(
            "canonical_field_mismatch",
            f"{path} 字段不一致: missing={sorted(expected - actual)} extra={sorted(actual - expected)}",
        )


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def validate_canonical_candidate(record: Mapping[str, Any]) -> None:
    """Strictly validate canonical shape, types, enum values and invariants."""
    if not isinstance(record, Mapping):
        raise CanonicalContractError("canonical_not_object", "canonical record 必须是对象")
    _require_exact_keys(
        record,
        {
            "schema_version",
            "source_identity_sha256",
            "conversation_sha256",
            "source",
            "conversation",
            "taxonomy",
            "signals",
            "features",
        },
        "$",
    )
    if record.get("schema_version") != CANONICAL_SCHEMA_VERSION:
        raise CanonicalContractError("canonical_schema_version", "canonical schema_version 非法")
    for key in ("source_identity_sha256", "conversation_sha256"):
        value = record.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise CanonicalContractError("canonical_invalid_hash", f"{key} 必须是 SHA-256")

    source = record.get("source")
    conversation = record.get("conversation")
    taxonomy = record.get("taxonomy")
    signals = record.get("signals")
    features = record.get("features")
    if not all(isinstance(value, Mapping) for value in (source, conversation, taxonomy, signals, features)):
        raise CanonicalContractError("canonical_invalid_section", "canonical 嵌套区块必须是对象")
    assert isinstance(source, Mapping) and isinstance(conversation, Mapping)
    assert isinstance(taxonomy, Mapping) and isinstance(signals, Mapping) and isinstance(features, Mapping)

    _require_exact_keys(
        source,
        {
            "source_snapshot_id",
            "source_line",
            "byte_offset",
            "byte_length",
            "record_sha256",
            "session_id",
            "raw_text",
            "trace",
        },
        "source",
    )
    for key in ("source_snapshot_id", "record_sha256"):
        value = source.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise CanonicalContractError("canonical_invalid_hash", f"source.{key} 必须是 SHA-256")
    for key, minimum in (("source_line", 1), ("byte_offset", 0), ("byte_length", 1)):
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise CanonicalContractError("canonical_invalid_locator", f"source.{key} 非法")
    for key in ("session_id", "raw_text"):
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CanonicalContractError("canonical_empty_source", f"source.{key} 不能为空")
    trace = source.get("trace")
    if not isinstance(trace, Mapping) or set(trace) != set(TRACE_FIELDS):
        raise CanonicalContractError("canonical_invalid_trace", "source.trace 字段不一致")
    try:
        normalize_trace_fields(trace)
    except CanonicalContractError as exc:
        raise CanonicalContractError("canonical_invalid_trace", str(exc)) from exc

    _require_exact_keys(
        conversation,
        {"full_text", "context_turns", "target_query", "turn_count", "sample_type"},
        "conversation",
    )
    full_text = conversation.get("full_text")
    context = conversation.get("context_turns")
    target = conversation.get("target_query")
    turn_count = conversation.get("turn_count")
    sample_type = conversation.get("sample_type")
    if not isinstance(full_text, str) or not full_text:
        raise CanonicalContractError("canonical_invalid_conversation", "conversation.full_text 不能为空")
    if not isinstance(context, list) or not all(isinstance(item, str) and item for item in context):
        raise CanonicalContractError("canonical_invalid_conversation", "context_turns 非法")
    if not isinstance(target, str) or not target:
        raise CanonicalContractError("canonical_invalid_conversation", "target_query 不能为空")
    if isinstance(turn_count, bool) or not isinstance(turn_count, int) or turn_count != len(context) + 1:
        raise CanonicalContractError("canonical_invalid_conversation", "turn_count 非法")
    expected_sample_type = "multi_turn" if context else "single_turn"
    if sample_type not in SAMPLE_TYPES or sample_type != expected_sample_type:
        raise CanonicalContractError("canonical_invalid_conversation", "sample_type 非法")
    # A marker may remain in the source-facing full_text while being removed from target.
    parsed = parse_conversation(str(source["raw_text"]))
    if dict(conversation) != parsed:
        raise CanonicalContractError("canonical_conversation_mismatch", "conversation 与 source.raw_text 不一致")
    if record["source_identity_sha256"] != source_identity_sha256(
        str(source["session_id"]), str(source["raw_text"])
    ):
        raise CanonicalContractError("canonical_identity_mismatch", "source identity 与源字段不一致")
    if record["conversation_sha256"] != conversation_sha256(conversation):
        raise CanonicalContractError("canonical_identity_mismatch", "conversation hash 与会话字段不一致")

    _require_exact_keys(taxonomy, {"cluster_id", "l1", "l2", "l2_key"}, "taxonomy")
    if isinstance(taxonomy.get("cluster_id"), bool) or not isinstance(taxonomy.get("cluster_id"), (int, str)):
        raise CanonicalContractError("canonical_invalid_taxonomy", "cluster_id 非法")
    for key in ("l1", "l2", "l2_key"):
        if not isinstance(taxonomy.get(key), str) or not taxonomy[key]:
            raise CanonicalContractError("canonical_invalid_taxonomy", f"taxonomy.{key} 不能为空")
    if taxonomy["l2_key"] != f"{taxonomy['l1']}/{taxonomy['l2']}":
        raise CanonicalContractError("canonical_invalid_taxonomy", "l2_key 非法")

    _require_exact_keys(signals, {"cluster_probability", "frequency"}, "signals")
    probability = signals.get("cluster_probability")
    frequency = signals.get("frequency")
    if not _is_number(probability) or not 0 <= float(probability) <= 1:
        raise CanonicalContractError("canonical_invalid_signals", "cluster_probability 非法")
    if (
        isinstance(frequency, bool)
        or not isinstance(frequency, int)
        or not 0 <= frequency <= MAX_SQLITE_INTEGER
    ):
        raise CanonicalContractError("canonical_invalid_signals", "frequency 非法")

    _require_exact_keys(
        features,
        {
            "probability_band",
            "frequency_band",
            "normalized_frequency",
            "relevance",
            "length_band",
            "complexity",
            "formality",
        },
        "features",
    )
    enum_checks = (
        ("probability_band", PROBABILITY_BANDS),
        ("frequency_band", FREQUENCY_BANDS),
        ("length_band", LENGTH_BANDS),
        ("complexity", COMPLEXITY_LEVELS),
        ("formality", FORMALITY_LEVELS),
    )
    for key, allowed in enum_checks:
        if features.get(key) not in allowed:
            raise CanonicalContractError("canonical_invalid_feature", f"features.{key} 非法")
    normalized = features.get("normalized_frequency")
    if not _is_number(normalized) or not 0 <= float(normalized) <= 1:
        raise CanonicalContractError("canonical_invalid_feature", "normalized_frequency 非法")
    relevance = features.get("relevance")
    if not isinstance(relevance, Mapping) or set(relevance) != {"p0", "p2"}:
        raise CanonicalContractError("canonical_invalid_feature", "features.relevance 非法")
    if not all(
        _is_number(relevance.get(key)) and 0.0 <= float(relevance[key]) <= 1.0
        for key in ("p0", "p2")
    ):
        raise CanonicalContractError("canonical_invalid_feature", "features.relevance 数值非法")

def frequency_stats_from_values(values: list[int], frequency: int) -> JsonObject:
    """Small-data helper used by tests and fixture construction."""
    if not values:
        raise CanonicalContractError("invalid_cluster_frequency_stats", "频率列表不能为空")
    ordered = sorted(values)
    return {
        "total_count": len(ordered),
        "less_count": bisect.bisect_left(ordered, frequency),
        "min_frequency": ordered[0],
        "max_frequency": ordered[-1],
    }
