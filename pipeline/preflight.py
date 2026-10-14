"""Early operational checks; never submit dataset text or change published files."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

from ..delivery.legacy import LEGACY_FILENAMES
from ..shared.llm import LlmCall, LlmConfig
from .config import PipelineConfig


PROBE_MESSAGE = "Reply with OK."


class PreflightError(RuntimeError):
    """Safe diagnostic that excludes provider messages, credentials and responses."""

    def __init__(self, check: str, operation: str, error_type: str, *,
                 path: Path | None = None, http_status: int | None = None,
                 errno: int | None = None, hint: str) -> None:
        details = {"check": check, "operation": operation, "error_type": error_type,
                   "hint": hint}
        if path is not None:
            details["path"] = str(path)
        if http_status is not None:
            details["http_status"] = http_status
        if errno is not None:
            details["errno"] = errno
        self.details = details
        super().__init__("preflight failed " + json.dumps(details, ensure_ascii=False, sort_keys=True))


def _log(logger: logging.Logger, level: int, check: str, status: str, **fields: Any) -> None:
    logger.log(level, "preflight %s", json.dumps(
        {"check": check, "status": status, **fields}, ensure_ascii=False, sort_keys=True,
    ))


def _failed(logger: logging.Logger, error: PreflightError, started: float) -> None:
    _log(logger, logging.ERROR, error.details["check"], "failed",
         **{key: value for key, value in error.details.items() if key != "check"},
         elapsed_ms=round((time.perf_counter() - started) * 1000, 2))


def check_directory(path: Path, role: str, logger: logging.Logger) -> dict[str, Any]:
    """Exercise create/write/replace/delete using exclusively owned temporary files."""
    started = time.perf_counter()
    operation = "mkdir"
    temporary: list[Path] = []
    _log(logger, logging.INFO, "directory", "started", role=role, path=str(path))
    try:
        path.mkdir(parents=True, exist_ok=True)
        operation = "create"
        descriptor, name = tempfile.mkstemp(dir=path, prefix=".screening-preflight-", suffix=".tmp")
        source = Path(name)
        temporary.append(source)
        operation = "write"
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"screening preflight\n")
            handle.flush()
            os.fsync(handle.fileno())
        operation = "create"
        descriptor, name = tempfile.mkstemp(dir=path, prefix=".screening-preflight-", suffix=".tmp")
        os.close(descriptor)
        destination = Path(name)
        temporary.append(destination)
        operation = "replace"
        os.replace(source, destination)
        operation = "delete"
        destination.unlink()
    except OSError as exc:
        error = PreflightError("directory", operation, type(exc).__name__, path=path,
                               errno=exc.errno, hint="check_path_permissions_space_and_file_locks")
        _failed(logger, error, started)
        raise error from None
    finally:
        for item in temporary:
            try:
                item.unlink(missing_ok=True)
            except OSError as exc:
                _log(logger, logging.WARNING, "directory_cleanup", "failed",
                     path=str(item), error_type=type(exc).__name__, errno=exc.errno)
    result = {"role": role, "path": str(path), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    _log(logger, logging.INFO, "directory", "passed", **result)
    return result


def check_publication_targets(root: Path, replace: bool, logger: logging.Logger) -> list[dict[str, Any]]:
    """Report existing files; final content equivalence remains a publication gate."""
    started = time.perf_counter()
    existing = []
    for set_name, relative in LEGACY_FILENAMES.items():
        destination = root / relative
        try:
            if not destination.exists():
                continue
            if not destination.is_file():
                raise IsADirectoryError("publication destination must be a file")
            with destination.open("rb") as handle:
                handle.read(1)
        except OSError as exc:
            error = PreflightError("publication_targets", "inspect", type(exc).__name__,
                                   path=destination, errno=exc.errno,
                                   hint="check_destination_type_and_read_permissions")
            _failed(logger, error, started)
            raise error from None
        item = {"set": set_name, "path": str(destination), "replace": replace,
                "next_action": "compare_hashes_after_export"}
        existing.append(item)
        _log(logger, logging.WARNING, "publication_targets", "exists_unverified", **item)
    _log(logger, logging.INFO, "publication_targets", "passed", existing_files=len(existing),
         next_action="final_hash_check_before_publish")
    return existing


def check_llm(config: LlmConfig, llm_call: LlmCall, logger: logging.Logger) -> dict[str, Any]:
    """One genuine request with the configured protocol and options; no retries."""
    started = time.perf_counter()
    metadata = config.public_metadata()
    _log(logger, logging.INFO, "llm", "started", model=config.model,
         endpoint_sha256=metadata["endpoint_sha256"], requests=1, timeout=config.timeout)
    operation = "request"
    try:
        content, first_latency, full_latency = llm_call(
            [{"role": "user", "content": PROBE_MESSAGE}], model=config.model, **config.options(),
        )
        operation = "response"
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty_or_invalid_response")
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        status = status if type(status) is int else None
        hint = {
            400: "check_endpoint_model_and_request_options",
            401: "check_api_key", 403: "check_api_key_and_model_permissions",
            404: "check_endpoint_and_model", 429: "check_rate_limit_or_quota",
        }.get(status, "check_service_network_timeout_and_response_protocol")
        if operation == "response":
            hint = "check_model_text_output_and_token_limit"
        error = PreflightError("llm", operation, type(exc).__name__, http_status=status, hint=hint)
        _failed(logger, error, started)
        raise error from None
    result = {"requests": 1, "model": config.model, "endpoint_sha256": metadata["endpoint_sha256"],
              "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    # Latencies are diagnostic only; never persist the probe's response text.
    if isinstance(first_latency, (int, float)) and isinstance(full_latency, (int, float)):
        result.update(first_token_latency_ms=first_latency, full_latency_ms=full_latency)
    _log(logger, logging.INFO, "llm", "passed", **result)
    return result


def run_preflight(config: PipelineConfig, *, prompts: Mapping[str, str],
                  llm_config: LlmConfig, llm_call: LlmCall,
                  replace: bool, logger: logging.Logger) -> dict[str, Any]:
    started = time.perf_counter()
    _log(logger, logging.INFO, "startup", "started")
    for stage, prompt in prompts.items():
        if prompt.count("{CANDIDATE_JSON}") != 1:
            error = PreflightError("prompt", "placeholder", "ValueError",
                                   hint=f"{stage}_requires_one_CANDIDATE_JSON_placeholder")
            _failed(logger, error, started)
            raise error
    _log(logger, logging.INFO, "prompt", "passed", stages=sorted(prompts))
    directories = [check_directory(path, role, logger) for role, path in (
        ("output", config.output_root), ("runs", config.output_root / "runs"),
        ("cache", config.cache_root),
    )]
    existing = []
    if config.publish_root is not None:
        directories.append(check_directory(config.publish_root, "publish", logger))
        for set_name, relative in LEGACY_FILENAMES.items():
            directories.append(check_directory((config.publish_root / relative).parent,
                                               f"publish_{set_name}", logger))
        existing = check_publication_targets(config.publish_root, replace, logger)
    else:
        _log(logger, logging.INFO, "publication_targets", "skipped", reason="publication_disabled")
    llm = check_llm(llm_config, llm_call, logger)
    result = {"status": "passed", "directories": directories, "existing_publication_files": existing,
              "llm": llm, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    _log(logger, logging.INFO, "startup", "passed", elapsed_ms=result["elapsed_ms"])
    return result
