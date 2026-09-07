"""Run the LLM pre-screen stage with auditable attempts and canonical results."""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from .core import (
    SET_FILES,
    JsonObject,
    build_candidate,
    ensure_within,
    iter_jsonl,
    load_prompt_template,
    non_negative_float,
    non_negative_int,
    parse_scores,
    prescreen_attempts_path,
    prescreen_results_path,
    positive_int,
    validate_selected_sets,
    validate_source_run_roots,
)
from .inference import BatchOutcome, LlmCall, run_inference_batch, validate_llm_environment
from .shared import chat_completion, setup_logger

logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = (
    PACKAGE_DIR / "artifacts" / "prompts" / "validity_filter_prompt.md"
)
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash/Cluster-1"
DEFAULT_LLM_OPTIONS: JsonObject = {
    "max_tokens": 32_768,
    "timeout": 300.0,
    "stream": True,
    "enable_thinking": False,
    "skip_special_tokens": False,
}


def render_record(
    set_name: str,
    source_line: int,
    row: JsonObject,
    template: str,
) -> JsonObject:
    candidate = build_candidate(row)
    candidate_json = json.dumps(candidate, ensure_ascii=False, indent=2)
    return {
        "filter_id": f"{set_name}_{source_line:08d}",
        "set": set_name,
        "source_file": str(SET_FILES[set_name]),
        "source_line": source_line,
        "session_id": row.get("session_id"),
        "quality_overall_score": row.get("quality_overall_score"),
        "candidate": candidate,
        "prompt": template.replace("{CANDIDATE_JSON}", candidate_json),
    }


def generate_prompts(
    source_root: Path,
    selected_sets: list[str],
    scores: set[int] | None,
    template: str,
    limit: int = 0,
    scores_by_set: dict[str, set[int]] | None = None,
) -> list[JsonObject]:
    """Build the deterministic pre-screen task list from immutable sources."""
    prompts: list[JsonObject] = []
    for set_name in selected_sets:
        active_scores = (
            scores_by_set[set_name]
            if scores_by_set is not None
            else scores
        )
        if not active_scores:
            raise ValueError(f"{set_name} 未配置质检分数范围")
        source_path = source_root / SET_FILES[set_name]
        if not source_path.is_file():
            raise FileNotFoundError(f"缺少 {set_name} 输入文件: {source_path}")
        for source_line, row in iter_jsonl(source_path):
            if row.get("quality_overall_score") not in active_scores:
                continue
            prompts.append(render_record(set_name, source_line, row, template))
            if limit and len(prompts) >= limit:
                return prompts
    return prompts


def run_prescreen(
    source_root: Path,
    run_root: Path,
    *,
    selected_sets: list[str],
    scores: set[int] | None = None,
    scores_by_set: dict[str, set[int]] | None = None,
    prompt_path: Path,
    model: str,
    workers: int,
    sleep_sec: float,
    max_retries: int,
    limit: int,
    llm_options: JsonObject | None = None,
    stage_logger: logging.Logger | None = None,
    llm_call: LlmCall = chat_completion,
) -> BatchOutcome:
    """Execute or resume pre-screening inside ``run_root``."""
    source_root, run_root = validate_source_run_roots(source_root, run_root)
    active_logger = stage_logger or logger
    template = load_prompt_template(prompt_path)
    records = generate_prompts(
        source_root,
        selected_sets,
        scores,
        template,
        limit,
        scores_by_set=scores_by_set,
    )
    active_logger.info("生成 %d 条统一质检任务", len(records))
    outcomes: list[BatchOutcome] = []
    for set_name in selected_sets:
        set_records = [record for record in records if record.get("set") == set_name]
        outcomes.append(
            run_inference_batch(
                set_records,
                stage=f"{set_name.lower()}_prescreen",
                attempts_path=prescreen_attempts_path(run_root, set_name),
                results_path=prescreen_results_path(run_root, set_name),
                model=model,
                workers=workers,
                sleep_sec=sleep_sec,
                max_retries=max_retries,
                llm_options=llm_options or DEFAULT_LLM_OPTIONS,
                selected_sets=[set_name],
                logger=active_logger,
                llm_call=llm_call,
            )
        )
    return BatchOutcome(
        expected=sum(item.expected for item in outcomes),
        already_completed=sum(item.already_completed for item in outcomes),
        completed_now=sum(item.completed_now for item in outcomes),
        parse_failures=sum(item.parse_failures for item in outcomes),
        engineering_failures=tuple(
            failure for item in outcomes for failure in item.engineering_failures
        ),
        fatal_error=next((item.fatal_error for item in outcomes if item.fatal_error), None),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 Query 有效性预筛")
    parser.add_argument("--source-root", type=Path, required=True, help="只读原始周目录")
    parser.add_argument("--run-root", type=Path, required=True, help="运行工作目录")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sets", default="P0,P1,P2")
    parser.add_argument(
        "--scores",
        type=parse_scores,
        default=None,
        help="兼容调试参数：指定后覆盖所有集合的分数范围",
    )
    parser.add_argument("--p0-scores", type=parse_scores, default={2, 3, 4, 5})
    parser.add_argument("--p1-scores", type=parse_scores, default={3, 4, 5})
    parser.add_argument("--p2-scores", type=parse_scores, default={3, 4, 5})
    parser.add_argument("--workers", type=positive_int, default=2)
    parser.add_argument("--sleep", type=non_negative_float, default=1.0)
    parser.add_argument("--max-retries", type=positive_int, default=3)
    parser.add_argument("--limit", type=non_negative_int, default=0)
    parser.add_argument("--max-tokens", type=positive_int, default=32_768)
    parser.add_argument("--timeout", type=non_negative_float, default=300.0)
    parser.add_argument(
        "--stream", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--enable-thinking", action=argparse.BooleanOptionalAction, default=False
    )
    parser.add_argument(
        "--skip-special-tokens",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    parser.add_argument("--log-file", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root, run_root = validate_source_run_roots(
        args.source_root, args.run_root
    )
    run_root.mkdir(parents=True, exist_ok=True)
    log_path = ensure_within(
        args.log_file or run_root / "logs" / "pipeline.log",
        run_root,
        "日志文件",
    )
    active_logger = setup_logger(
        __name__,
        args.log_level,
        log_path,
        run_id=run_root.name,
        stage="prescreen",
    )
    try:
        selected_sets = validate_selected_sets(args.sets)
        scores_by_set = {
            "P0": args.scores or args.p0_scores,
            "P1": args.scores or args.p1_scores,
            "P2": args.scores or args.p2_scores,
        }
        template = load_prompt_template(args.prompt.expanduser().resolve())
        if args.generate_only:
            records = generate_prompts(
                source_root,
                selected_sets,
                args.scores,
                template,
                args.limit,
                scores_by_set=scores_by_set,
            )
            active_logger.info("预筛任务总数: %d", len(records))
            active_logger.info("各集合任务数: %s", dict(Counter(r["set"] for r in records)))
            return 0
        validate_llm_environment()
        outcome = run_prescreen(
            source_root,
            run_root,
            selected_sets=selected_sets,
            scores=args.scores,
            scores_by_set=scores_by_set,
            prompt_path=args.prompt.expanduser().resolve(),
            model=args.model,
            workers=args.workers,
            sleep_sec=args.sleep,
            max_retries=args.max_retries,
            limit=args.limit,
            llm_options={
                "max_tokens": args.max_tokens,
                "timeout": args.timeout,
                "stream": args.stream,
                "enable_thinking": args.enable_thinking,
                "skip_special_tokens": args.skip_special_tokens,
            },
            stage_logger=active_logger,
        )
        active_logger.info(
            "预筛完成度: expected=%d, existing=%d, new=%d, parse_fail=%d, call_fail=%d",
            outcome.expected,
            outcome.already_completed,
            outcome.completed_now,
            outcome.parse_failures,
            len(outcome.engineering_failures),
        )
        return 0 if outcome.complete else 2
    except (OSError, ValueError, RuntimeError) as exc:
        active_logger.error("预筛失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
