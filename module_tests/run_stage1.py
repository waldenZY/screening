"""Re-evaluate delivered labeled JSONL with the current Stage1 prompt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# Allow both direct script execution and python -m screening.module_tests.run_stage1.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from screening.quality.two_stage import (
    QUALITY_CONTRACT_VERSION,
    QualityResponseError,
    stage1_passes,
    validate_stage1_result,
)
from screening.module_tests.xlsx_export import validate_xlsx_dependency, write_results_xlsx
from screening.shared.json_io import sha256_file, try_parse_json, write_json
from screening.shared.llm import (
    DEFAULT_LLM_CONFIG_PATH,
    PACKAGE_ROOT,
    LlmCall,
    LlmConfig,
    LlmRequestError,
    load_llm_config,
    validate_llm_dependency,
)

DEFAULT_PROMPT_PATH = PACKAGE_ROOT / "artifacts" / "prompts" / "quality_stage1.md"
NOTICE = "使用当前 Prompt 重新判断交付样本，不保证逐字还原历史 Stage1 请求；不计算准确率。"


def build_stage1_input(record: Any) -> dict[str, Any]:
    """Restore only the fields Stage1 consumes, without cleaning or label leakage."""
    if not isinstance(record, dict):
        raise ValueError("输入行必须是 JSON 对象")
    for field in ("text", "target_query", "L1", "L2"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"{field} 必须是非空字符串")
    context = record.get("context_text")
    if not isinstance(context, str):
        raise ValueError("context_text 必须是字符串，单轮时为空字符串")
    turns = (context.split(" | ") if context else []) + [record["target_query"]]
    if any(not turn.strip() for turn in turns):
        raise ValueError("会话中不能存在空轮次")
    if record["text"] != " | ".join(turns):
        raise ValueError("text 与 context_text、target_query 不一致")
    if type(record.get("turn_count")) is not int or record["turn_count"] != len(turns):
        raise ValueError("turn_count 与恢复后的轮次数不一致")
    return {
        "sample_type": "multi_turn" if len(turns) > 1 else "single_turn",
        "conversation_turns": turns,
        "candidate_category": {"L1": record["L1"], "L2": record["L2"]},
    }


def _read_samples(path: Path, limit: int | None) -> list[dict[str, Any]]:
    samples = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            sample = {"source_line": line_number, "source_record": None, "source_text": None}
            try:
                record = json.loads(line)
                # Keep malformed Unicode and non-finite numbers from breaking output later.
                json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
                sample["source_record"] = record
            except (ValueError, UnicodeError):
                sample["source_text"] = line.rstrip("\r\n")
            samples.append(sample)
            if limit is not None and len(samples) >= limit:
                break
    if not samples:
        raise ValueError("输入文件没有非空样本行")
    return samples


def _evaluate(
    sample: dict[str, Any], prompt: str, config: LlmConfig, llm_call: LlmCall
) -> dict[str, Any]:
    result = {
        **sample,
        "status": "input_error",
        "stage1_input": None,
        "raw_response": None,
        "stage1_result": None,
        "stage1_pass": None,
        "elapsed_ms": None,
        "first_token_latency_ms": None,
        "full_latency_ms": None,
        "error": None,
    }
    try:
        if sample["source_text"] is not None:
            raise ValueError("输入行不是可保存的标准 UTF-8 JSON")
        payload = build_stage1_input(sample["source_record"])
    except ValueError as exc:
        result["error"] = {"type": "input_error", "message": str(exc)}
        return result

    result["stage1_input"] = payload
    rendered = prompt.replace("{CANDIDATE_JSON}", json.dumps(payload, ensure_ascii=False, indent=2))
    started = time.perf_counter()
    try:
        raw, first_latency, full_latency = llm_call(
            [{"role": "user", "content": rendered}], model=config.model, **config.options()
        )
        if not isinstance(raw, str):
            raise TypeError("LLM response content must be a string")
    except Exception as exc:
        result["status"] = "request_error"
        # Arbitrary client exceptions can contain URLs, headers or credentials.
        message = (
            str(exc) if isinstance(exc, LlmRequestError)
            else "模型请求或响应读取失败，未输出可能含连接信息的异常详情"
        )
        result["error"] = {"type": type(exc).__name__, "message": message}
        return result
    finally:
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)

    result.update(raw_response=raw, first_token_latency_ms=first_latency, full_latency_ms=full_latency)
    try:
        success, parsed = try_parse_json(raw)
        if not success:
            raise QualityResponseError("响应不是合法 JSON 对象")
        validated = validate_stage1_result(parsed)
    except QualityResponseError as exc:
        result["status"] = "response_error"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        return result
    result.update(status="ok", stage1_result=validated, stage1_pass=stage1_passes(validated))
    return result


def _fenced(text: str, language: str = "json") -> str:
    fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}"


def _write_report(path: Path, info: dict[str, Any], results: list[dict[str, Any]]) -> None:
    counts = info["counts"]
    lines = [
        "# Stage1 独立复测结果", "", NOTICE, "",
        f"运行状态：{info['status']}；模型：{info['llm']['model']}", "",
        f"选中 {counts['selected']} 条，已处理 {counts['processed']} 条；"
        f"通过 {counts['passed']} 条，拒绝 {counts['rejected']} 条，失败 {counts['failed']} 条。", "",
    ]
    for index, result in enumerate(results, start=1):
        decision = {True: "通过", False: "拒绝", None: "未判定"}[result["stage1_pass"]]
        lines.extend([
            f"## 样本 {index}（输入第 {result['source_line']} 行）", "",
            f"状态：{result['status']}；Stage1 准入：{decision}；耗时：{result['elapsed_ms']} ms", "",
            "实际模型输入：", "",
            _fenced(json.dumps(result["stage1_input"], ensure_ascii=False, indent=2)), "",
            "校验后的判断：", "",
            _fenced(json.dumps(result["stage1_result"], ensure_ascii=False, indent=2)), "",
        ])
        if result["status"] == "input_error":
            original = result["source_text"]
            if original is None:
                original = json.dumps(result["source_record"], ensure_ascii=False, indent=2)
            lines.extend(["原始输入：", "", _fenced(original, "text"), ""])
        if result["error"] is not None:
            lines.extend(["错误：", "", _fenced(json.dumps(result["error"], ensure_ascii=False, indent=2)), ""])
        if result["raw_response"] is not None:
            lines.extend(["模型原始响应：", "", _fenced(result["raw_response"], "text"), ""])
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def run_stage1_test(
    input_path: Path,
    output_dir: Path,
    *,
    llm_config: LlmConfig,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
    limit: int | None = None,
    llm_call: LlmCall | None = None,
) -> int:
    """Run fresh, single-attempt requests; an injected client supports offline checks."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit 必须是正整数")
    input_path, output_dir = input_path.expanduser().resolve(), output_dir.expanduser().resolve()
    prompt_path = prompt_path.expanduser().resolve()
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ValueError("输出目录必须不存在或为空；不会覆盖已有内容")
    samples = _read_samples(input_path, limit)
    prompt = prompt_path.read_text(encoding="utf-8")
    if prompt.count("{CANDIDATE_JSON}") != 1:
        raise ValueError("Stage1 Prompt 必须恰好包含一个 {CANDIDATE_JSON}")
    validate_xlsx_dependency()
    info = {
        "module": "stage1",
        "status": "running",
        "notice": NOTICE,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "input_path": str(input_path),
        "input_sha256": sha256_file(input_path),
        "prompt_path": str(prompt_path),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "quality_contract_version": QUALITY_CONTRACT_VERSION,
        "llm": llm_config.public_metadata(),
        "limit": limit,
        "attempts_per_record": 1,
        "cache_enabled": False,
        "counts": {"selected": len(samples), "processed": 0, "passed": 0, "rejected": 0, "failed": 0},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also prevents two invocations from claiming the same empty directory.
    with (output_dir / "run.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(info, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with (output_dir / "stage1_prompt.md").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(prompt)
    client = llm_call if llm_call is not None else llm_config.bind()
    results = []
    interrupted = False
    with (output_dir / "results.jsonl").open("x", encoding="utf-8", newline="\n") as handle:
        try:
            for sample in samples:
                result = _evaluate(sample, prompt, llm_config, client)
                handle.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                results.append(result)
                print(
                    f"[{len(results)}/{len(samples)}] line={result['source_line']} "
                    f"status={result['status']} stage1_pass={result['stage1_pass']}"
                )
        except KeyboardInterrupt:
            interrupted = True
    info["counts"].update(
        processed=len(results),
        passed=sum(result["stage1_pass"] is True for result in results),
        rejected=sum(result["stage1_pass"] is False for result in results),
        failed=sum(result["status"] != "ok" for result in results),
    )
    info["status"] = (
        "interrupted" if interrupted
        else "completed_with_errors" if info["counts"]["failed"] else "completed"
    )
    info["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output_dir / "run.json", info)
    _write_report(output_dir / "report.md", info, results)
    write_results_xlsx(output_dir / "results.xlsx", results)
    print(f"结果目录：{output_dir}")
    return 130 if interrupted else 2 if info["counts"]["failed"] else 0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单独复测最终 labeled JSONL 的 Stage1 输出，不计算准确率")
    parser.add_argument("--input", type=Path, required=True, help="抽出的 labeled JSONL 文件")
    parser.add_argument("--output-dir", type=Path, required=True, help="本次结果目录，必须不存在或为空")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH, help="Stage1 Prompt，默认使用当前生产文件")
    parser.add_argument("--llm-config", type=Path, default=DEFAULT_LLM_CONFIG_PATH, help="LLM 配置，默认使用现有本地配置")
    parser.add_argument("--limit", type=_positive_int, help="只处理前 N 个非空行，含错误行和重复行")
    args = parser.parse_args(argv)
    try:
        config = load_llm_config(args.llm_config)
        validate_llm_dependency()
        return run_stage1_test(
            args.input, args.output_dir, llm_config=config,
            prompt_path=args.prompt, limit=args.limit,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        # Model exceptions are handled per record; startup messages contain no config values.
        message = "文件不是有效 UTF-8 文本" if isinstance(exc, UnicodeError) else str(exc)
        print(f"Stage1 复测启动或文件写入失败：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
