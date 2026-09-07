"""Small client for an OpenAI-compatible chat-completions endpoint."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from typing import Any

DEFAULT_MODEL = "Qwen_3_5_w8a8_slow"
DEFAULT_MAX_TOKENS = 32_768
DEFAULT_TIMEOUT_SECONDS = 300.0


def _required_environment_value(name: str, explicit_value: str | None) -> str:
    value = explicit_value or os.getenv(name, "")
    if not value or value == "xxxx":
        raise RuntimeError(f"缺少配置：请设置环境变量 {name}")
    return value


def _extract_non_stream_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 返回结构不符合预期: {payload!r}") from exc
    if not isinstance(content, str):
        raise RuntimeError("LLM 返回的 message.content 不是字符串")
    return content


def chat_completion(
    messages: Sequence[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    stream: bool = True,
    enable_thinking: bool = False,
    skip_special_tokens: bool = False,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, float, float]:
    """Call the endpoint and return text plus first-token and total latency in ms."""
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
        raise RuntimeError(
            "缺少 requests 依赖；请在运行环境中安装 requests 后再调用 LLM"
        ) from exc

    resolved_url = _required_environment_value("LLM_API_URL", api_url)
    resolved_api_key = _required_environment_value("LLM_API_KEY", api_key)
    headers = {
        "Authorization": f"Bearer {resolved_api_key}",
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
    response = requests.post(
        resolved_url,
        headers=headers,
        json=payload,
        stream=stream,
        timeout=timeout,
    )
    response.raise_for_status()

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
