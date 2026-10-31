"""Inspect reference/labeled samples or replay policy; never infer accuracy."""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from screening.quality.runner import QUALITY_CONTRACT_VERSION
from screening.quality.admission import (DEFAULT_POLICY, QualityResponseError, build_admission_result,
    validate_features, validate_category, validate_category_response, validate_policy, validate_admission_result,
    admission_passes, stable_hash, admission_group, admission_issue_summary)
from screening.module_tests.xlsx_export import validate_xlsx_dependency, write_results_xlsx
from screening.shared.json_io import sha256_file, try_parse_json, write_json
from screening.shared.llm import (DEFAULT_LLM_CONFIG_PATH, PACKAGE_ROOT, LlmCall, LlmConfig,
    LlmRequestError, load_llm_config, validate_llm_dependency)

DEFAULT_PROMPT_PATH = PACKAGE_ROOT / "artifacts/prompts/quality_admission.md"
DEFAULT_CATEGORY_PROMPT_PATH = PACKAGE_ROOT / "artifacts/prompts/quality_admission_category.md"
NOTICE = "参考样本复测；已有类别或标签不是人工真值，不计算准确率。文本筛选通过不代表正式用例已就绪。"


def build_admission_input(record: Any) -> dict[str, Any]:
    """Normalize two supported input shapes and reject conflicting duplicate fields."""
    if not isinstance(record, dict):
        raise ValueError("输入行必须是 JSON 对象")
    if isinstance(record.get("candidate"), dict):
        from screening.candidate.contract import parse_conversation
        candidate = record["candidate"]
        conversation = parse_conversation(candidate["source"]["raw_text"])
        if conversation != candidate["conversation"]:
            raise ValueError("冻结候选的会话与原文不一致")
        return {"sample_type": conversation["sample_type"],
            "conversation_turns": conversation["full_text"].split(" | "),
            "candidate_category": {"L1": candidate["taxonomy"]["l1"], "L2": candidate["taxonomy"]["l2"]}}
    target, context = record.get("target_query"), record.get("context_text")
    if not isinstance(target, str) or not target.strip() or not isinstance(context, str):
        raise ValueError("target_query 必须非空，context_text 必须为字符串")
    expected_turns = (context.split(" | ") if context else []) + [target]
    turns = record.get("conversation_turns", expected_turns)
    if not isinstance(turns, list) or not turns or any(not isinstance(t, str) or not t.strip() for t in turns):
        raise ValueError("conversation_turns 必须是非空文本数组")
    if turns != expected_turns:
        raise ValueError("conversation_turns 与 context_text、target_query 不一致")
    sample_type = "multi_turn" if len(turns) > 1 else "single_turn"
    if "sample_type" in record and record["sample_type"] != sample_type:
        raise ValueError("sample_type 与轮次不一致")
    if "turn_count" in record and (type(record["turn_count"]) is not int or record["turn_count"] != len(turns)):
        raise ValueError("turn_count 与轮次不一致")
    if "text" in record and record["text"] != " | ".join(turns):
        raise ValueError("text 与会话字段不一致")
    category = record.get("candidate_category", {"L1": record.get("L1"), "L2": record.get("L2")})
    if not isinstance(category, dict) or set(category) != {"L1", "L2"}:
        raise ValueError("candidate_category 必须含 L1、L2")
    values = list(category.values())
    if not all(x is None for x in values) and not all(isinstance(x, str) and x.strip() for x in values):
        raise ValueError("类别必须同时提供非空 L1/L2，或同时为空")
    for key in ("L1", "L2"):
        if key in record and record[key] != category[key]:
            raise ValueError("两种类别字段不一致")
    if isinstance(record.get("raw_text"), str) and isinstance(record.get("stage1"), dict):
        from screening.candidate.contract import parse_conversation
        original = parse_conversation(record["raw_text"])
        if (original["context_turns"] != (context.split(" | ") if context else [])
                or original["target_query"] != target or original["sample_type"] != sample_type):
            raise ValueError("交付会话与raw_text不一致")
        turns = original["full_text"].split(" | ")
    return {"sample_type": sample_type, "conversation_turns": list(turns), "candidate_category": dict(category)}


def _read_samples(path: Path, limit: int | None) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = {"source_line": number, "source_record": None, "source_text": None}
            try:
                record = json.loads(line)
                json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
                row["source_record"] = record
            except (ValueError, UnicodeError):
                row["source_text"] = line.rstrip("\r\n")
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    if not rows:
        raise ValueError("输入文件没有非空样本行")
    return rows


def _evaluate(sample: dict, feature_prompt: str, category_prompt: str, config: LlmConfig | None,
              client: LlmCall | None, policy: dict, *, replay: bool = False) -> dict:
    result = {**sample, "status": "input_error", "admission_input": None, "feature_input": None,
        "category_input": None, "features": None, "category_review": None,
        "admission_result": None, "admission_pass": None, "route": None, "admission_group": None, "raw_response": {},
        "stage_status": {"features": "not_run", "category": "not_run"}, "observation_provenance": None,
        "elapsed_ms": 0.0, "first_token_latency_ms": 0.0, "full_latency_ms": 0.0, "error": None}
    previous = None
    try:
        if sample["source_text"] is not None:
            raise ValueError("输入不是可保存的 UTF-8 JSON")
        if replay:
            previous = sample["source_record"]
            if not isinstance(previous, dict) or not isinstance(previous.get("source_record"), dict):
                raise ValueError("重筛输入必须是独立复测产生的 results.jsonl")
            result["source_record"] = previous["source_record"]
            result["source_line"] = previous["source_line"]
        payload = build_admission_input(result["source_record"])
        if previous is not None and previous.get("admission_input") != payload:
            raise ValueError("重筛输入与原始会话绑定不一致")
    except (ValueError, KeyError, TypeError) as exc:
        result["error"] = {"type": "input_error", "message": str(exc)}
        return result
    feature_input = {k: payload[k] for k in ("sample_type", "conversation_turns")}
    category_input = payload if payload["candidate_category"]["L1"] is not None else None
    result.update(admission_input=payload, feature_input=feature_input, category_input=category_input)
    started = time.perf_counter()
    current_stage = "features"
    try:
        if previous is not None:
            if previous.get("features") is None or previous.get("category_review") is None:
                raise QualityResponseError("原记录缺少成功的独立判断，不能无模型重筛")
            provenance = previous.get("observation_provenance")
            if (not isinstance(provenance, dict)
                    or provenance.get("feature_input_sha256") != stable_hash(feature_input)
                    or provenance.get("category_input_sha256") != stable_hash(category_input)):
                raise QualityResponseError("原模型观察与输入哈希不一致")
            prior_result = validate_admission_result(previous.get("admission_result"))["admission"]
            features = validate_features(previous["features"])
            category = validate_category(previous["category_review"])
            if features != prior_result["features"] or category != prior_result["category"]:
                raise QualityResponseError("原独立判断与聚合结果不一致")
            result["observation_provenance"] = previous.get("observation_provenance")
            result["raw_response"] = previous.get("raw_response", {})
            result["stage_status"] = {"features": "replayed", "category": "replayed" if category_input else "not_provided"}
        else:
            assert config is not None and client is not None
            result["observation_provenance"] = {"feature_input_sha256": stable_hash(feature_input),
                "category_input_sha256": stable_hash(category_input), "feature_prompt_sha256": stable_hash(feature_prompt),
                "category_prompt_sha256": stable_hash(category_prompt), "llm": config.public_metadata()}
            def call(stage: str, prompt: str, data: dict) -> Any:
                raw, first, full = client([{"role": "user", "content": prompt.replace("{CANDIDATE_JSON}", json.dumps(data, ensure_ascii=False, indent=2))}], model=config.model, **config.options())
                if not isinstance(raw, str):
                    raise QualityResponseError("响应必须是文本")
                result["raw_response"][stage] = raw
                result["first_token_latency_ms"] += first
                result["full_latency_ms"] += full
                ok, parsed = try_parse_json(raw)
                if not ok:
                    raise QualityResponseError("响应不是合法 JSON 对象")
                return parsed
            features = validate_features(call("features", feature_prompt, feature_input))
            result["features"] = features
            result["stage_status"]["features"] = "ok"
            current_stage = "category"
            category = validate_category_response(call("category", category_prompt, category_input)) if category_input else {"status": "not_provided", "fit": None}
            result["stage_status"]["category"] = "ok" if category_input else "not_provided"
        if (category_input is None) != (category["status"] == "not_provided"):
            raise QualityResponseError("类别判断与是否提供类别不一致")
        result.update(features=features, category_review=category)
        admission = build_admission_result(features, category, policy)
        result.update(status="ok", admission_result=admission, admission_pass=admission_passes(admission), route=admission["admission"]["screening"]["route"], admission_group=admission_group(admission))
    except QualityResponseError as exc:
        result.update(status="response_error", error={"type": type(exc).__name__, "message": str(exc)})
        result["stage_status"][current_stage] = "response_error"
    except Exception as exc:
        result.update(status="request_error", error={"type": type(exc).__name__,
            "message": str(exc) if isinstance(exc, LlmRequestError) else "模型请求失败，连接信息未输出"})
        result["stage_status"][current_stage] = "request_error"
    finally:
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _write_report(path: Path, info: dict, results: list[dict]) -> None:
    lines = ["# 准入评估 特征与策略复测", "", NOTICE, "", f"模式：{info['mode']}", "",
             "缺少类别不等于语义无效；route 是文本筛选类型，admission_pass 包含类别与策略要求。", "",
             "统计：", "", "```json", json.dumps(info["counts"], ensure_ascii=False, indent=2), "```", ""]
    for row in results:
        lines += [f"## 输入第 {row['source_line']} 行", "", f"状态：{row['status']}；路线：{row['route']}；准入：{row['admission_pass']}；分组：{row['admission_group']}", ""]
        # Indented blocks prevent embedded query Markdown from closing a fence.
        lines += ["\n".join("    " + line for line in json.dumps({k: row[k] for k in ("source_record", "admission_result", "error")}, ensure_ascii=False, indent=2).splitlines()), ""]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def run_admission_test(input_path: Path, output_dir: Path, *, llm_config: LlmConfig | None = None,
    prompt_path: Path = DEFAULT_PROMPT_PATH, category_prompt_path: Path = DEFAULT_CATEGORY_PROMPT_PATH,
    policy: dict | None = None, limit: int | None = None, llm_call: LlmCall | None = None, replay: bool = False) -> int:
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit 必须是正整数")
    policy = validate_policy(DEFAULT_POLICY if policy is None else policy)
    input_path, output_dir = input_path.resolve(), output_dir.resolve()
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise ValueError("输出目录必须不存在或为空")
    samples = _read_samples(input_path, limit)
    feature_prompt = prompt_path.read_text(encoding="utf-8")
    category_prompt = category_prompt_path.read_text(encoding="utf-8")
    if any(p.count("{CANDIDATE_JSON}") != 1 for p in (feature_prompt, category_prompt)):
        raise ValueError("两个 Prompt 必须各含一个 {CANDIDATE_JSON}")
    if not replay and llm_config is None:
        raise ValueError("真实复测需要 LLM 配置")
    validate_xlsx_dependency()
    client = None if replay else (llm_call or llm_config.bind())
    info = {"module": "admission", "mode": "policy_replay" if replay else "model_review", "status": "running",
        "notice": NOTICE, "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        "input_path": str(input_path), "input_sha256": sha256_file(input_path),
        "quality_contract_version": QUALITY_CONTRACT_VERSION, "screening_policy": policy,
        "policy_sha256": stable_hash(policy), "llm": None if replay else llm_config.public_metadata(),
        "feature_prompt_sha256": stable_hash(feature_prompt), "category_prompt_sha256": stable_hash(category_prompt),
        "limit": limit, "attempts_per_stage": 0 if replay else 1, "cache_enabled": False, "counts": {}}
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "run.json").open("x", encoding="utf-8") as handle:
        json.dump(info, handle, ensure_ascii=False, indent=2)
    (output_dir / "admission_prompt.md").write_text(feature_prompt, encoding="utf-8", newline="\n")
    (output_dir / "category_prompt.md").write_text(category_prompt, encoding="utf-8", newline="\n")
    results, interrupted = [], False
    with (output_dir / "results.jsonl").open("x", encoding="utf-8", newline="\n") as handle:
        try:
            for sample in samples:
                row = _evaluate(sample, feature_prompt, category_prompt, llm_config, client, policy, replay=replay)
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                results.append(row)
                print(f"[{len(results)}/{len(samples)}] line={row['source_line']} status={row['status']} route={row['route']} admitted={row['admission_pass']}")
        except KeyboardInterrupt:
            interrupted = True
    info["counts"] = {"selected": len(samples), "processed": len(results),
        "passed": sum(r["admission_pass"] is True for r in results), "rejected": sum(r["admission_pass"] is False for r in results),
        "failed": sum(r["status"] != "ok" for r in results),
        **{route: sum(r["route"] == route for r in results) for route in ("direct", "clarification", "excluded")},
        "category_not_provided": sum(r["category_review"] == {"status": "not_provided", "fit": None} for r in results),
        **admission_issue_summary([r["admission_result"] for r in results if r["status"] == "ok"])}
    info["status"] = "interrupted" if interrupted else "completed_with_errors" if info["counts"]["failed"] else "completed"
    info["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output_dir / "run.json", info)
    _write_report(output_dir / "report.md", info, results)
    write_results_xlsx(output_dir / "results.xlsx", results)
    return 130 if interrupted else 2 if info["counts"]["failed"] else 0


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=NOTICE)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="reference 或 labeled JSONL")
    group.add_argument("--replay-results", type=Path, help="已有 results.jsonl；只重跑策略，不调用模型")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH)
    parser.add_argument("--category-prompt", type=Path, default=DEFAULT_CATEGORY_PROMPT_PATH)
    parser.add_argument("--policy", type=Path, help="筛选策略 JSON；默认生产策略")
    parser.add_argument("--llm-config", type=Path, default=DEFAULT_LLM_CONFIG_PATH)
    parser.add_argument("--limit", type=_positive_int)
    args = parser.parse_args(argv)
    try:
        replay = args.replay_results is not None
        config = None
        if not replay:
            config = load_llm_config(args.llm_config)
            validate_llm_dependency()
        policy = json.loads(args.policy.read_text(encoding="utf-8-sig")) if args.policy else None
        return run_admission_test(args.replay_results or args.input, args.output_dir, llm_config=config,
            prompt_path=args.prompt, category_prompt_path=args.category_prompt, policy=policy, limit=args.limit, replay=replay)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"准入评估 启动或文件写入失败：{exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
