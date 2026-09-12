"""Fixed local configuration and client for an OpenAI-compatible endpoint."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LLM_CONFIG_PATH = PACKAGE_ROOT / "artifacts" / "config" / "llm_config.local.json"
EXAMPLE_LLM_CONFIG_PATH = PACKAGE_ROOT / "artifacts" / "config" / "llm_config.example.json"

LlmCall = Callable[..., tuple[str, float, float]]

_CONFIG_FIELDS = {
    "api_url",
    "api_key",
    "model",
    "max_tokens",
    "timeout",
    "stream",
    "enable_thinking",
    "skip_special_tokens",
}


class LlmRequestError(RuntimeError):
    """Sanitized transport error that never includes endpoint or credentials."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        message = f"LLM HTTP request failed ({status_code})" if status_code else "LLM request failed"
        super().__init__(message)


@dataclass(frozen=True, repr=False)
class LlmConfig:
    """Validated local LLM settings; connection secrets stay memory-only."""

    api_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str
    max_tokens: int
    timeout: float
    stream: bool
    enable_thinking: bool
    skip_special_tokens: bool

    def __repr__(self) -> str:
        return f"LlmConfig(model={self.model!r}, connection=<redacted>)"

    def public_metadata(self) -> dict[str, Any]:
        """Return the reproducibility fields safe to persist in a manifest."""
        return {
            "endpoint_sha256": hashlib.sha256(self.api_url.encode("utf-8")).hexdigest(),
            "model": self.model,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "stream": self.stream,
            "enable_thinking": self.enable_thinking,
            "skip_special_tokens": self.skip_special_tokens,
        }

    def options(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "stream": self.stream,
            "enable_thinking": self.enable_thinking,
            "skip_special_tokens": self.skip_special_tokens,
        }

    def bind(self) -> Callable[..., tuple[str, float, float]]:
        def configured_call(*args: Any, **kwargs: Any) -> tuple[str, float, float]:
            return chat_completion(
                *args,
                **kwargs,
                api_url=self.api_url,
                api_key=self.api_key,
            )

        return configured_call


def _configuration_error(message: str) -> ValueError:
    return ValueError(
        f"LLM 本地配置非法: {message}；请复制 {EXAMPLE_LLM_CONFIG_PATH.name} "
        f"为 {DEFAULT_LLM_CONFIG_PATH.name} 后填写"
    )


def load_llm_config(path: Path = DEFAULT_LLM_CONFIG_PATH) -> LlmConfig:
    """Load the one fixed, git-ignored LLM configuration file."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"缺少 LLM 本地配置: {resolved}；请复制 {EXAMPLE_LLM_CONFIG_PATH.name} "
            f"为 {DEFAULT_LLM_CONFIG_PATH.name} 后填写"
        )
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _configuration_error("不是合法 JSON") from exc
    if not isinstance(value, dict) or set(value) != _CONFIG_FIELDS:
        raise _configuration_error(f"字段必须且只能是 {', '.join(sorted(_CONFIG_FIELDS))}")

    api_url = value["api_url"]
    api_key = value["api_key"]
    model = value["model"]
    if not isinstance(api_url, str) or not api_url.strip():
        raise _configuration_error("api_url 必须是非空字符串")
    api_url = api_url.strip()
    parsed_url = urlsplit(api_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
        or "replace-me" in api_url.lower()
    ):
        raise _configuration_error("api_url 必须是未嵌入凭证且不含 fragment 的 HTTP(S) 地址")
    if (
        not isinstance(api_key, str)
        or not api_key.strip()
        or api_key.strip().lower() in {"replace-me", "xxxx"}
    ):
        raise _configuration_error("api_key 不能为空或保留占位值")
    if not isinstance(model, str) or not model.strip():
        raise _configuration_error("model 必须是非空字符串")

    max_tokens = value["max_tokens"]
    timeout = value["timeout"]
    if type(max_tokens) is not int or max_tokens < 1:
        raise _configuration_error("max_tokens 必须是正整数")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise _configuration_error("timeout 必须是正数")
    for name in ("stream", "enable_thinking", "skip_special_tokens"):
        if type(value[name]) is not bool:
            raise _configuration_error(f"{name} 必须是布尔值")

    return LlmConfig(
        api_url=api_url,
        api_key=api_key.strip(),
        model=model.strip(),
        max_tokens=max_tokens,
        timeout=float(timeout),
        stream=value["stream"],
        enable_thinking=value["enable_thinking"],
        skip_special_tokens=value["skip_special_tokens"],
    )


def validate_llm_dependency() -> None:
    """Fail before expensive preparation when the HTTP dependency is absent."""
    try:
        import requests  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 requests 依赖；请先安装 requirements.txt") from exc


def _extract_non_stream_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 返回结构不符合预期: {payload!r}") from exc
    if not isinstance(content, str):
        raise RuntimeError("LLM 返回的 message.content 不是字符串")
    return content


def chat_completion(
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    stream: bool,
    enable_thinking: bool,
    skip_special_tokens: bool,
    *,
    api_url: str,
    api_key: str,
    timeout: float,
) -> tuple[str, float, float]:
    """Call the configured endpoint and return text and latency values in ms."""
    if not messages:
        raise ValueError("messages 不能为空")
    if not model.strip():
        raise ValueError("model 不能为空")
    if max_tokens < 1:
        raise ValueError("max_tokens 必须大于等于 1")
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")

    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 requests 依赖；请先安装 requirements.txt") from exc

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": list(messages),
        "extra_body": {
            "enable_thinking": enable_thinking,
            "skip_special_tokens": skip_special_tokens,
        },
    }

    start_time = time.perf_counter()
    try:
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            stream=stream,
            timeout=timeout,
        )
        response.raise_for_status()
    except Exception as exc:
        response_value = getattr(exc, "response", None)
        status_code = getattr(response_value, "status_code", None)
        raise LlmRequestError(status_code if isinstance(status_code, int) else None) from None

    if not stream:
        result = _extract_non_stream_content(response.json())
        full_latency = round((time.perf_counter() - start_time) * 1000, 2)
        return result, full_latency, full_latency

    result_parts: list[str] = []
    first_token_time: float | None = None
    for raw_line in response.iter_lines(decode_unicode=True):
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8")
        if not raw_line or not raw_line.startswith("data:"):
            continue
        data = raw_line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
            content = chunk["choices"][0]["delta"].get("content")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"无法解析 LLM 流式响应: {data!r}") from exc
        if content is None:
            continue
        if not isinstance(content, str):
            raise RuntimeError("LLM 流式响应中的 content 不是字符串")
        if first_token_time is None:
            first_token_time = time.perf_counter()
        result_parts.append(content)

    end_time = time.perf_counter()
    full_latency = round((end_time - start_time) * 1000, 2)
    first_token_latency = (
        round((first_token_time - start_time) * 1000, 2)
        if first_token_time is not None
        else full_latency
    )
    return "".join(result_parts), first_token_latency, full_latency
