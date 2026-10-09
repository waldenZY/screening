"""Strict, path-stable configuration profile for the screening v3 entrypoint."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ..quality.two_stage import QualityRunConfig
from ..shared.json_io import JsonObject
from .orchestrator import BUILD_CONFIG_VERSION, BuildConfig, parse_build_config


PIPELINE_CONFIG_VERSION = 4
LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

_TOP_LEVEL_FIELDS = {
    "config_version",
    "inputs",
    "outputs",
    "prompts",
    "quality",
    "index",
    "logging",
    "publication",
    "sampling",
    "p1_supplement",
    "p2_supplement",
}
_P0_FIELDS = {
    "l1_big_threshold",
    "l2_per_l1_big",
    "l2_per_l1_small",
    "max_per_cluster",
    "fallback_pool_size",
    "mmr_lambda",
    "dedup_threshold",
}
_P1_FIELDS = {
    "target_n",
    "probability_weights",
    "frequency_weights",
    "cold_boundary_floor",
    "mmr_lambda",
    "dedup_threshold",
}
_P2_FIELDS = {
    "target_n",
    "candidate_source_shares",
    "minimum_complexity",
    "boundary_probability_cap",
    "soft_min_per_type",
    "max_per_primary",
    "batch_size",
    "dedup_threshold",
}
_SUPPLEMENT_FIELDS = {
    "max_rounds",
    "default_pass_rate",
    "pass_rate_prior_strength",
    "minimum_pass_rate",
    "maximum_pass_rate",
    "safety_factor",
}
_P2_SUPPLEMENT_FIELDS = {
    "max_rounds",
    "default_yield",
    "type_default_yield",
    "prior_strength",
    "minimum_yield",
    "safety_factor",
    "exploration_share",
}
_OVERRIDE_FIELDS = {
    "source",
    "taxonomy",
    "risk_keywords",
    "output_root",
    "stage1_prompt",
    "challenge_prompt",
    "stage2_prompt",
    "workers",
    "max_retries",
    "sleep_seconds",
    "cache_root",
    "index_batch_size",
    "index_progress_every",
    "index_cache_mb",
    "index_min_free_space_gb",
    "index_reserve_free_space_gb",
    "verify_full_source_hash",
    "log_level",
    "publication_enabled",
    "publish_root",
}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing:
        raise ValueError(f"{label} 缺少字段: {sorted(missing)}")
    if extra:
        raise ValueError(f"{label} 包含未知字段: {sorted(extra)}")


def _integer(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的整数")
    return value


def _number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的数值")
    return float(value)


def _path(
    value: Any,
    label: str,
    base_dir: Path,
    *,
    optional: bool = False,
) -> Path | None:
    if value is None or value == "":
        if optional:
            return None
        raise ValueError(f"{label} 不能为空")
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{label} 必须是路径字符串")
    normalized = value.strip() if isinstance(value, str) else value
    if not normalized:
        raise ValueError(f"{label} 不能为空")
    raw = Path(normalized).expanduser()
    return (raw if raw.is_absolute() else base_dir / raw).resolve()


@dataclass(frozen=True)
class IndexRuntimeConfig:
    cache_root: Path | None
    batch_size: int
    progress_every: int
    sqlite_cache_mb: int
    min_free_space_gb: float
    reserve_free_space_gb: float
    verify_full_source_hash: bool

    def __post_init__(self) -> None:
        _integer(self.batch_size, "index.batch_size")
        _integer(self.progress_every, "index.progress_every")
        _integer(self.sqlite_cache_mb, "index.sqlite_cache_mb")
        _number(self.min_free_space_gb, "index.min_free_space_gb")
        _number(self.reserve_free_space_gb, "index.reserve_free_space_gb")
        if self.min_free_space_gb < self.reserve_free_space_gb:
            raise ValueError("index.min_free_space_gb 不能小于 reserve_free_space_gb")
        if type(self.verify_full_source_hash) is not bool:
            raise ValueError("index.verify_full_source_hash 必须是布尔值")


@dataclass(frozen=True)
class PublicationConfig:
    enabled: bool
    publish_root: Path | None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("publication.enabled 必须是布尔值")


@dataclass(frozen=True)
class PipelineConfig:
    config_version: int
    config_path: Path
    source: Path | None
    taxonomy: Path | None
    risk_keywords: Path | None
    output_root: Path
    stage1_prompt: Path
    challenge_prompt: Path
    stage2_prompt: Path
    quality: QualityRunConfig
    index: IndexRuntimeConfig
    log_level: str
    publication: PublicationConfig
    build: BuildConfig

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    @property
    def cache_root(self) -> Path:
        return self.index.cache_root or (self.output_root / "_canonical_cache").resolve()

    @property
    def publish_root(self) -> Path | None:
        return self.publication.publish_root if self.publication.enabled else None

    def validate_for_execution(self) -> None:
        for label, path in (
            ("inputs.source", self.source),
            ("inputs.taxonomy", self.taxonomy),
            ("inputs.risk_keywords", self.risk_keywords),
            ("prompts.stage1", self.stage1_prompt),
            ("prompts.challenge", self.challenge_prompt),
            ("prompts.stage2", self.stage2_prompt),
        ):
            if path is None:
                raise ValueError(f"{label} 不能为空；请在配置文件中填写或使用 CLI 覆盖")
            if not path.is_file():
                raise FileNotFoundError(f"{label} 文件不存在: {path}")
        if self.publication.enabled and self.publication.publish_root is None:
            raise ValueError("publication.enabled=true 时 publish_root 不能为空")

    def manifest_value(self, *, run_id: str | None, new_run: bool, replace_files: bool) -> JsonObject:
        """Return the effective, non-secret invocation settings for audit."""
        return {
            "pipeline_config_version": self.config_version,
            "config_path": str(self.config_path),
            "inputs": {
                "source": None if self.source is None else str(self.source),
                "taxonomy": None if self.taxonomy is None else str(self.taxonomy),
                "risk_keywords": None if self.risk_keywords is None else str(self.risk_keywords),
            },
            "outputs": {"output_root": str(self.output_root)},
            "prompts": {
                "stage1": str(self.stage1_prompt),
                "challenge": str(self.challenge_prompt),
                "stage2": str(self.stage2_prompt),
            },
            "quality": asdict(self.quality),
            "index": {
                "cache_root": str(self.cache_root),
                "batch_size": self.index.batch_size,
                "progress_every": self.index.progress_every,
                "sqlite_cache_mb": self.index.sqlite_cache_mb,
                "min_free_space_gb": self.index.min_free_space_gb,
                "reserve_free_space_gb": self.index.reserve_free_space_gb,
                "verify_full_source_hash": self.index.verify_full_source_hash,
            },
            "logging": {"level": self.log_level},
            "publication": {
                "enabled": self.publication.enabled,
                "publish_root": (
                    None
                    if self.publication.publish_root is None
                    else str(self.publication.publish_root)
                ),
            },
            "build_config": asdict(self.build),
            "one_shot": {
                "run_id": run_id,
                "new_run": new_run,
                "replace": replace_files,
            },
        }


def _parse_build(value: Mapping[str, Any]) -> BuildConfig:
    sampling = _object(value["sampling"], "sampling")
    _exact_fields(sampling, {"p0", "p1", "p2"}, "sampling")
    p0 = _object(sampling["p0"], "sampling.p0")
    p1 = _object(sampling["p1"], "sampling.p1")
    p2 = _object(sampling["p2"], "sampling.p2")
    _exact_fields(p0, _P0_FIELDS, "sampling.p0")
    _exact_fields(p1, _P1_FIELDS, "sampling.p1")
    _exact_fields(p2, _P2_FIELDS, "sampling.p2")
    if not isinstance(p1["probability_weights"], dict):
        raise ValueError("sampling.p1.probability_weights 必须是对象")
    if not isinstance(p1["frequency_weights"], dict):
        raise ValueError("sampling.p1.frequency_weights 必须是对象")
    if not isinstance(p2["candidate_source_shares"], dict):
        raise ValueError("sampling.p2.candidate_source_shares 必须是对象")
    supplement = _object(value["p1_supplement"], "p1_supplement")
    _exact_fields(supplement, _SUPPLEMENT_FIELDS, "p1_supplement")
    p2_supplement = _object(value["p2_supplement"], "p2_supplement")
    _exact_fields(p2_supplement, _P2_SUPPLEMENT_FIELDS, "p2_supplement")
    return parse_build_config(
        {
            "config_version": BUILD_CONFIG_VERSION,
            "sampling": sampling,
            "p1_supplement": supplement,
            "p2_supplement": p2_supplement,
        }
    )


def load_pipeline_config(path: Path) -> PipelineConfig:
    """Load and strictly validate one complete v3 execution profile."""
    config_path = path.expanduser().resolve()
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"pipeline 配置不是合法 JSON: {config_path}") from exc
    value = _object(value, "pipeline 配置")
    _exact_fields(value, _TOP_LEVEL_FIELDS, "pipeline 配置")
    if value["config_version"] != PIPELINE_CONFIG_VERSION:
        raise ValueError(
            f"不支持的 pipeline 配置版本: {value['config_version']}; "
            f"当前版本为 {PIPELINE_CONFIG_VERSION}"
        )

    inputs = _object(value["inputs"], "inputs")
    outputs = _object(value["outputs"], "outputs")
    prompts = _object(value["prompts"], "prompts")
    quality = _object(value["quality"], "quality")
    index = _object(value["index"], "index")
    logging_value = _object(value["logging"], "logging")
    publication = _object(value["publication"], "publication")
    _exact_fields(inputs, {"source", "taxonomy", "risk_keywords"}, "inputs")
    _exact_fields(outputs, {"output_root"}, "outputs")
    _exact_fields(prompts, {"stage1", "challenge", "stage2"}, "prompts")
    _exact_fields(quality, {"workers", "max_retries", "sleep_seconds"}, "quality")
    _exact_fields(
        index,
        {
            "cache_root",
            "batch_size",
            "progress_every",
            "sqlite_cache_mb",
            "min_free_space_gb",
            "reserve_free_space_gb",
            "verify_full_source_hash",
        },
        "index",
    )
    _exact_fields(logging_value, {"level"}, "logging")
    _exact_fields(publication, {"enabled", "publish_root"}, "publication")

    workers = _integer(quality["workers"], "quality.workers")
    max_retries = _integer(quality["max_retries"], "quality.max_retries")
    sleep_seconds = _number(quality["sleep_seconds"], "quality.sleep_seconds")
    level = logging_value["level"]
    if not isinstance(level, str) or level.upper() not in LOG_LEVELS:
        raise ValueError(f"logging.level 必须是以下枚举之一: {sorted(LOG_LEVELS)}")
    if type(publication["enabled"]) is not bool:
        raise ValueError("publication.enabled 必须是布尔值")
    if type(index["verify_full_source_hash"]) is not bool:
        raise ValueError("index.verify_full_source_hash 必须是布尔值")

    base_dir = config_path.parent
    return PipelineConfig(
        config_version=PIPELINE_CONFIG_VERSION,
        config_path=config_path,
        source=_path(inputs["source"], "inputs.source", base_dir, optional=True),
        taxonomy=_path(inputs["taxonomy"], "inputs.taxonomy", base_dir, optional=True),
        risk_keywords=_path(inputs["risk_keywords"], "inputs.risk_keywords", base_dir, optional=True),
        output_root=_path(outputs["output_root"], "outputs.output_root", base_dir),  # type: ignore[arg-type]
        stage1_prompt=_path(prompts["stage1"], "prompts.stage1", base_dir),  # type: ignore[arg-type]
        challenge_prompt=_path(prompts["challenge"], "prompts.challenge", base_dir),  # type: ignore[arg-type]
        stage2_prompt=_path(prompts["stage2"], "prompts.stage2", base_dir),  # type: ignore[arg-type]
        quality=QualityRunConfig(
            workers=workers,
            max_retries=max_retries,
            sleep_seconds=sleep_seconds,
        ),
        index=IndexRuntimeConfig(
            cache_root=_path(index["cache_root"], "index.cache_root", base_dir, optional=True),
            batch_size=_integer(index["batch_size"], "index.batch_size"),
            progress_every=_integer(index["progress_every"], "index.progress_every"),
            sqlite_cache_mb=_integer(index["sqlite_cache_mb"], "index.sqlite_cache_mb"),
            min_free_space_gb=_number(index["min_free_space_gb"], "index.min_free_space_gb"),
            reserve_free_space_gb=_number(
                index["reserve_free_space_gb"], "index.reserve_free_space_gb"
            ),
            verify_full_source_hash=index["verify_full_source_hash"],
        ),
        log_level=level.upper(),
        publication=PublicationConfig(
            enabled=publication["enabled"],
            publish_root=_path(
                publication["publish_root"],
                "publication.publish_root",
                base_dir,
                optional=True,
            ),
        ),
        build=_parse_build(value),
    )


def apply_pipeline_overrides(
    config: PipelineConfig,
    overrides: Mapping[str, Any],
) -> PipelineConfig:
    """Apply explicit CLI values; relative CLI paths use the config directory."""
    unknown = set(overrides) - _OVERRIDE_FIELDS
    if unknown:
        raise ValueError(f"未知 CLI 配置覆盖字段: {sorted(unknown)}")
    values = {key: value for key, value in overrides.items() if value is not None}
    base_dir = config.config_dir

    quality = replace(
        config.quality,
        workers=values.get("workers", config.quality.workers),
        max_retries=values.get("max_retries", config.quality.max_retries),
        sleep_seconds=values.get("sleep_seconds", config.quality.sleep_seconds),
    )
    index = replace(
        config.index,
        cache_root=(
            _path(values["cache_root"], "--cache-root", base_dir)
            if "cache_root" in values
            else config.index.cache_root
        ),
        batch_size=values.get("index_batch_size", config.index.batch_size),
        progress_every=values.get("index_progress_every", config.index.progress_every),
        sqlite_cache_mb=values.get("index_cache_mb", config.index.sqlite_cache_mb),
        min_free_space_gb=values.get(
            "index_min_free_space_gb", config.index.min_free_space_gb
        ),
        reserve_free_space_gb=values.get(
            "index_reserve_free_space_gb", config.index.reserve_free_space_gb
        ),
        verify_full_source_hash=values.get(
            "verify_full_source_hash", config.index.verify_full_source_hash
        ),
    )
    publication = replace(
        config.publication,
        enabled=values.get("publication_enabled", config.publication.enabled),
        publish_root=(
            _path(values["publish_root"], "--publish-root", base_dir)
            if "publish_root" in values
            else config.publication.publish_root
        ),
    )
    level = values.get("log_level", config.log_level)
    if not isinstance(level, str) or level.upper() not in LOG_LEVELS:
        raise ValueError(f"--log-level 必须是以下枚举之一: {sorted(LOG_LEVELS)}")
    return replace(
        config,
        source=(
            _path(values["source"], "--source", base_dir)
            if "source" in values
            else config.source
        ),
        taxonomy=(
            _path(values["taxonomy"], "--taxonomy", base_dir)
            if "taxonomy" in values
            else config.taxonomy
        ),
        risk_keywords=(
            _path(values["risk_keywords"], "--risk-keywords", base_dir)
            if "risk_keywords" in values
            else config.risk_keywords
        ),
        output_root=(
            _path(values["output_root"], "--output-root", base_dir)  # type: ignore[arg-type]
            if "output_root" in values
            else config.output_root
        ),
        stage1_prompt=(
            _path(values["stage1_prompt"], "--stage1-prompt", base_dir)  # type: ignore[arg-type]
            if "stage1_prompt" in values
            else config.stage1_prompt
        ),
        challenge_prompt=(
            _path(values["challenge_prompt"], "--challenge-prompt", base_dir)  # type: ignore[arg-type]
            if "challenge_prompt" in values
            else config.challenge_prompt
        ),
        stage2_prompt=(
            _path(values["stage2_prompt"], "--stage2-prompt", base_dir)  # type: ignore[arg-type]
            if "stage2_prompt" in values
            else config.stage2_prompt
        ),
        quality=quality,
        index=index,
        log_level=level.upper(),
        publication=publication,
    )


def configure_logging(level: str) -> None:
    """Initialize deterministic console logging for the standalone entrypoint."""
    normalized = level.upper()
    if normalized not in LOG_LEVELS:
        raise ValueError(f"非法日志等级: {level}")
    logging.basicConfig(
        level=getattr(logging, normalized),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
