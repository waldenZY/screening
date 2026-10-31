"""Independently review Stage2 downstream quality using existing admission evidence."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from screening.module_tests.run_admission import build_admission_input, _read_samples, _positive_int
from screening.module_tests.xlsx_export import validate_xlsx_dependency, write_annotation_results_xlsx
from screening.quality.admission import QualityResponseError, stable_hash, validate_admission_result
from screening.quality.annotation import QUALITY_RUBRIC_VERSION, validate_quality_annotation
from screening.quality.runner import QUALITY_CONTRACT_VERSION
from screening.shared.json_io import sha256_file, try_parse_json, write_json
from screening.shared.llm import (DEFAULT_LLM_CONFIG_PATH, PACKAGE_ROOT, LlmConfig, LlmCall,
    LlmRequestError, load_llm_config, validate_llm_dependency)

DEFAULT_PROMPT_PATH = PACKAGE_ROOT / "artifacts/prompts/quality_annotation.md"
NOTICE = "下游质量补充标注独立复测（Stage2子模块）；只复核已有准入依据，不重新准入、不改变成员或执行标签，不计算准确率。"


def build_quality_input(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("输入行必须是JSON对象")
    if "admission_input" in record and "source_record" in record:
        # Admission module output must still be bound to the actual original input.
        payload = build_admission_input(record["source_record"])
        if record.get("status") != "ok" or record["admission_input"] != payload:
            raise ValueError("准入复测未成功，或原始会话与准入输入不一致")
        admission = validate_admission_result(record.get("admission_result"))["admission"]
        if record.get("features") != admission["features"] or record.get("category_review") != admission["category"]:
            raise ValueError("准入独立判断与聚合结果不一致")
        provenance = record.get("observation_provenance")
        feature_input = {key: payload[key] for key in ("sample_type", "conversation_turns")}
        category_input = payload if payload["candidate_category"]["L1"] is not None else None
        if (not isinstance(provenance, dict)
                or provenance.get("feature_input_sha256") != stable_hash(feature_input)
                or provenance.get("category_input_sha256") != stable_hash(category_input)):
            raise ValueError("准入观察与原始输入哈希不一致")
    elif isinstance(record.get("candidate"), dict):
        from screening.delivery.screened import validate_screened_canonical_record
        validate_screened_canonical_record(record)
        payload = build_admission_input(record)
        admission = validate_admission_result({"admission": record["quality"]["admission"]})["admission"]
    elif isinstance(record.get("stage1"), dict):
        payload = build_admission_input(record)
        admission = validate_admission_result({"admission": record["stage1"].get("admission")})["admission"]
    else:
        raise ValueError("需要准入复测results.jsonl、Stage1冻结成品、内部成品或含stage1.admission的下游文件；请先获取准入判断")
    category = payload["candidate_category"]
    if (any(not isinstance(category[key], str) or not category[key].strip() for key in ("L1", "L2"))
            or admission["category"]["status"] != "reviewed"):
        raise ValueError("下游质量复测需要真实候选L1/L2及已完成的类别核验，不补造缺失类别")
    return {**payload, "admission": admission, "quality_rubric_version": QUALITY_RUBRIC_VERSION}


def _evaluate(sample: dict, prompt: str, config: LlmConfig, client: LlmCall) -> dict:
    row = {**sample, "status": "input_error", "annotation_input": None, "quality_result": None,
        "raw_response": None, "observation_provenance": None, "error": None,
        "elapsed_ms": 0.0, "first_token_latency_ms": 0.0, "full_latency_ms": 0.0}
    try:
        if sample["source_text"] is not None:
            raise ValueError("输入不是可保存的UTF-8 JSON")
        payload = build_quality_input(sample["source_record"])
        row["annotation_input"] = payload
    except (ValueError, KeyError, TypeError) as exc:
        row["error"] = {"type": "input_error", "message": str(exc)}
        return row
    row["observation_provenance"] = {"input_sha256": stable_hash(payload),
        "prompt_sha256": stable_hash(prompt), "quality_rubric_version": QUALITY_RUBRIC_VERSION,
        "llm": config.public_metadata()}
    started = time.perf_counter()
    try:
        raw, first, full = client(
            [{"role": "user", "content": prompt.replace("{CANDIDATE_JSON}", json.dumps(payload, ensure_ascii=False, indent=2))}],
            model=config.model, **config.options())
        row.update(raw_response=raw, first_token_latency_ms=first, full_latency_ms=full)
        if not isinstance(raw, str):
            row["raw_response"] = None
            raise QualityResponseError("响应必须是文本")
        ok, parsed = try_parse_json(raw)
        if not ok:
            raise QualityResponseError("响应不是合法JSON对象")
        row.update(status="ok", quality_result=validate_quality_annotation(parsed))
    except QualityResponseError as exc:
        row.update(status="response_error", error={"type": type(exc).__name__, "message": str(exc)})
    except Exception as exc:
        row.update(status="request_error", error={"type": type(exc).__name__,
            "message": str(exc) if isinstance(exc, LlmRequestError) else "模型请求失败，连接信息未输出"})
    finally:
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return row


def run_quality_test(input_path: Path, output_dir: Path, *, llm_config: LlmConfig,
                     prompt_path: Path = DEFAULT_PROMPT_PATH, limit: int | None = None,
                     llm_call: LlmCall | None = None) -> int:
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit必须为正整数")
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ValueError("输出目录必须不存在或为空")
    samples = _read_samples(input_path, limit)
    prompt = prompt_path.read_text(encoding="utf-8")
    if prompt.count("{CANDIDATE_JSON}") != 1:
        raise ValueError("质量Prompt必须恰含一个 {CANDIDATE_JSON}")
    validate_xlsx_dependency()
    client = llm_call or llm_config.bind()
    info = {"module": "quality_annotation", "phase": "stage2", "status": "running", "notice": NOTICE,
        "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        "input_path": str(input_path), "input_sha256": sha256_file(input_path),
        "quality_contract_version": QUALITY_CONTRACT_VERSION, "quality_rubric_version": QUALITY_RUBRIC_VERSION,
        "prompt_sha256": stable_hash(prompt), "llm": llm_config.public_metadata(),
        "attempts_per_stage": 1, "cache_enabled": False, "limit": limit, "counts": {}}
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "run.json", info)
    (output_dir / "quality_annotation_prompt.md").write_text(prompt, encoding="utf-8", newline="\n")
    results, interrupted = [], False
    with (output_dir / "results.jsonl").open("x", encoding="utf-8", newline="\n") as handle:
        try:
            for sample in samples:
                row = _evaluate(sample, prompt, llm_config, client)
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n");handle.flush()
                results.append(row)
                score = (row["quality_result"] or {}).get("quality_overall_score")
                print(f"[{len(results)}/{len(samples)}] line={row['source_line']} status={row['status']} score={score}")
        except KeyboardInterrupt:
            interrupted = True
    succeeded = [row["quality_result"] for row in results if row["status"] == "ok"]
    info["counts"] = {"selected": len(samples), "processed": len(results), "succeeded": len(succeeded),
        "failed": len(results)-len(succeeded), "low_score": sum(row["quality_overall_score"] < 5 for row in succeeded),
        "score_distribution": {str(score): sum(row["quality_overall_score"] == score for row in succeeded) for score in range(1,6)},
        "issue_distribution": dict(sorted(Counter(issue for row in succeeded for issue in row["quality_issue_types"]).items()))}
    info["status"] = "interrupted" if interrupted else "completed_with_errors" if info["counts"]["failed"] else "completed"
    info["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output_dir / "run.json", info)
    lines = ["# 下游质量补充标注独立复测", "", NOTICE, "", "    "+json.dumps(info["counts"],ensure_ascii=False), ""]
    for row in results:
        lines += [f"## 输入第{row['source_line']}行：{row['status']}", "",
            "\n".join("    "+line for line in json.dumps({key: row[key] for key in ("annotation_input", "quality_result", "error")},ensure_ascii=False,indent=2).splitlines()), ""]
    (output_dir / "report.md").write_text("\n".join(lines),encoding="utf-8",newline="\n")
    write_annotation_results_xlsx(output_dir / "results.xlsx", results)
    return 130 if interrupted else 2 if info["counts"]["failed"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument("--input",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--prompt",type=Path,default=DEFAULT_PROMPT_PATH)
    parser.add_argument("--llm-config",type=Path,default=DEFAULT_LLM_CONFIG_PATH)
    parser.add_argument("--limit",type=_positive_int)
    args = parser.parse_args(argv)
    try:
        config=load_llm_config(args.llm_config);validate_llm_dependency()
        return run_quality_test(args.input,args.output_dir,llm_config=config,prompt_path=args.prompt,limit=args.limit)
    except (OSError,ValueError,RuntimeError) as exc:
        print(f"下游质量复测启动或文件写入失败：{exc}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
