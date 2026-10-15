"""Offline checks of the standalone runner; no real model requests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from screening.module_tests import run_stage1 as runner
from screening.shared.llm import LlmConfig, LlmRequestError


PASSED = {
    "category_fit": "exact",
    "semantic_completeness": "self_contained",
    "intent_clarity": "clear",
    "validity": "valid",
    "blocking_issues": [],
}
REJECTED = {
    **PASSED,
    "semantic_completeness": "context_dependent",
    "validity": "invalid",
    "blocking_issues": ["attachment_dependency"],
}
CONFIG = LlmConfig(
    api_url="https://offline.example.invalid/v1/chat/completions",
    api_key="offline-secret-do-not-save",
    model="offline-model",
    max_tokens=512,
    timeout=3.0,
    stream=False,
    enable_thinking=False,
    skip_special_tokens=False,
)


def labeled(target: str = "测试目标", context: str = "") -> dict:
    return {
        "text": f"{context} | {target}" if context else target,
        "context_text": context,
        "target_query": target,
        "turn_count": len(context.split(" | ")) + 1 if context else 1,
        "L1": "测试一级",
        "L2": "测试二级",
        "cluster_id": 1,
        "cluster_prob": 0.9,
        "frequency": 2,
        "session_id": "same-session",
        "set": "P1_benchmark",
        "quality_task_difficulty": "old-difficulty-must-not-leak",
        "intent_label": "old-intent-must-not-leak",
        "executable": 0,
    }


class Stage1ModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="screening_stage1_test_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "samples.jsonl"
        self.output = self.root / "output"
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("RULES\n{CANDIDATE_JSON}\nEND\n", encoding="utf-8")

    def write_samples(self, records: list) -> None:
        self.input.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def run_samples(self, client: Mock | None = None, **kwargs) -> tuple[int, Mock]:
        if client is None:
            client = Mock(return_value=(json.dumps(PASSED), 1.25, 2.5))
        with contextlib.redirect_stdout(io.StringIO()):
            code = runner.run_stage1_test(
                self.input, self.output, llm_config=CONFIG,
                prompt_path=self.prompt, llm_call=client, **kwargs,
            )
        return code, client

    def read_results(self) -> list[dict]:
        return [json.loads(line) for line in (self.output / "results.jsonl").read_text(encoding="utf-8").splitlines()]

    def read_info(self) -> dict:
        return json.loads((self.output / "run.json").read_text(encoding="utf-8"))

    def artifact_text(self, artifact: Path) -> str:
        if artifact.suffix != ".xlsx":
            return artifact.read_text(encoding="utf-8")
        workbook = load_workbook(artifact)
        try:
            return "\n".join(
                str(cell.value)
                for sheet in workbook for row in sheet for cell in row
                if cell.value is not None
            )
        finally:
            workbook.close()

    def test_preserve_exact_turns_and_isolate_old_labels(self) -> None:
        records = [labeled("  [(图片)]目标  "), labeled("改成乙", "第一轮 | 第二轮")]
        records[1]["set"] = "P2_adversarial"
        self.write_samples(records)
        code, client = self.run_samples()
        self.assertEqual(code, 0)
        results = self.read_results()
        self.assertEqual(results[0]["stage1_input"]["conversation_turns"], ["  [(图片)]目标  "])
        self.assertEqual(results[1]["stage1_input"], {
            "sample_type": "multi_turn",
            "conversation_turns": ["第一轮", "第二轮", "改成乙"],
            "candidate_category": {"L1": "测试一级", "L2": "测试二级"},
        })
        for source, result, call in zip(records, results, client.call_args_list):
            self.assertEqual(result["source_record"], source)
            rendered = call.args[0][0]["content"]
            self.assertEqual(json.loads(rendered.removeprefix("RULES\n").removesuffix("\nEND\n")), result["stage1_input"])
            self.assertNotIn(source["intent_label"], rendered)
            self.assertNotIn(source["quality_task_difficulty"], rendered)
            self.assertNotIn("executable", rendered)
            self.assertEqual(call.kwargs, {"model": CONFIG.model, **CONFIG.options()})

    def test_bad_records_do_not_invoke_model(self) -> None:
        mutations = [
            {"text": "不一致"}, {"turn_count": True}, {"turn_count": 2},
            {"context_text": None}, {"L1": " "}, {"target_query": ""},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runner.build_stage1_input({**labeled(), **mutation})
        self.write_samples([labeled(), None, {"target_query": "缺字段"}, {**labeled(), "text": "不一致"}])
        code, client = self.run_samples()
        self.assertEqual(code, 2)
        self.assertEqual(client.call_count, 1)
        for result in self.read_results()[1:]:
            self.assertEqual(result["status"], "input_error")
            self.assertIsNone(result["stage1_pass"])
            self.assertIsNone(result["stage1_input"])

    def test_partial_response_failures_are_not_rejections(self) -> None:
        self.write_samples([labeled(str(i)) for i in range(5)])
        conflict = {**PASSED, "blocking_issues": ["attachment_dependency"]}
        client = Mock(side_effect=[
            (f"```json\n{json.dumps(PASSED)}\n```", 1.0, 2.0),
            (json.dumps(REJECTED), 1.0, 2.0),
            ("not JSON", 1.0, 2.0),
            (json.dumps(conflict), 1.0, 2.0),
            LlmRequestError(429),
        ])
        code, _ = self.run_samples(client)
        self.assertEqual(code, 2)
        self.assertEqual(client.call_count, 5)
        results = self.read_results()
        self.assertEqual([r["stage1_pass"] for r in results], [True, False, None, None, None])
        self.assertEqual([r["status"] for r in results], ["ok", "ok", "response_error", "response_error", "request_error"])
        self.assertEqual(results[2]["raw_response"], "not JSON")
        self.assertIsNone(results[4]["raw_response"])
        info = self.read_info()
        self.assertEqual(info["counts"], {"selected": 5, "processed": 5, "passed": 1, "rejected": 1, "failed": 3})
        self.assertEqual(info["status"], "completed_with_errors")
        self.assertIn("通过 1 条，拒绝 1 条，失败 3 条", (self.output / "report.md").read_text(encoding="utf-8"))
        workbook = load_workbook(self.output / "results.xlsx")
        try:
            sheet = workbook["Stage1结果"]
            values = list(sheet.values)
            rows = [dict(zip(values[0], row)) for row in values[1:]]
            self.assertEqual([row["stage1_pass"] for row in rows], [1, 0, None, None, None])
            self.assertEqual([row["source_line"] for row in rows], [1, 2, 3, 4, 5])
            self.assertEqual(rows[1]["blocking_issues"], '["attachment_dependency"]')
            self.assertEqual(rows[2]["raw_response"], "not JSON")
            self.assertEqual(rows[4]["status"], "request_error")
            self.assertEqual(sheet.freeze_panes, "E2")
            self.assertEqual(sheet.auto_filter.ref, sheet.dimensions)
        finally:
            workbook.close()

    def test_bom_blanks_invalid_json_limit_and_line_numbers(self) -> None:
        valid = json.dumps(labeled(), ensure_ascii=False)
        self.input.write_text(f"\n{{bad json\n\n{valid}\n{valid}\n", encoding="utf-8-sig")
        code, client = self.run_samples(limit=2)
        self.assertEqual(code, 2)
        self.assertEqual(client.call_count, 1)
        results = self.read_results()
        self.assertEqual([r["source_line"] for r in results], [2, 4])
        self.assertEqual(results[0]["source_text"], "{bad json")
        self.assertEqual(results[0]["status"], "input_error")

    def test_duplicates_and_repeated_runs_make_fresh_requests(self) -> None:
        self.write_samples([labeled(), labeled()])
        code, client = self.run_samples()
        self.assertEqual(code, 0)
        self.assertEqual(client.call_count, 2)
        self.output = self.root / "another_output"
        code, _ = self.run_samples(client)
        self.assertEqual(code, 0)
        self.assertEqual(client.call_count, 4)
        self.assertEqual(len(self.read_results()), 2)

    def test_nonempty_directory_and_file_are_never_overwritten(self) -> None:
        self.write_samples([labeled()])
        self.output.mkdir()
        original = self.output / "keep.txt"
        original.write_text("keep", encoding="utf-8")
        client = Mock()
        with self.assertRaisesRegex(ValueError, "不会覆盖"):
            self.run_samples(client)
        self.assertEqual(original.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.output.iterdir()), [original])
        self.output = self.root / "existing_file"
        self.output.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_samples(client)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep")
        client.assert_not_called()

    def test_preexisting_empty_output_and_complete_artifacts(self) -> None:
        self.write_samples([labeled()])
        self.output.mkdir()
        code, _ = self.run_samples()
        self.assertEqual(code, 0)
        self.assertEqual(
            {p.name for p in self.output.iterdir()},
            {"results.jsonl", "results.xlsx", "report.md", "run.json", "stage1_prompt.md"},
        )
        self.assertEqual(
            (self.output / "stage1_prompt.md").read_text(encoding="utf-8"),
            self.prompt.read_text(encoding="utf-8"),
        )
        info = self.read_info()
        self.assertEqual(info["input_sha256"], runner.sha256_file(self.input))
        self.assertEqual(info["prompt_sha256"], runner.sha256_file(self.output / "stage1_prompt.md"))
        self.assertEqual(info["llm"], CONFIG.public_metadata())
        self.assertEqual(info["attempts_per_record"], 1)
        self.assertFalse(info["cache_enabled"])

    def test_invalid_prompt_empty_input_and_invalid_limit_fail_before_writes(self) -> None:
        self.write_samples([labeled()])
        client = Mock()
        for content in ("no placeholder", "{CANDIDATE_JSON}{CANDIDATE_JSON}"):
            with self.subTest(content=content):
                self.prompt.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_samples(client)
                self.assertFalse(self.output.exists())
        for limit in (0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.run_samples(client, limit=limit)
        self.input.write_text("\n \n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "没有非空"):
            self.run_samples(client)
        self.assertFalse(self.output.exists())
        client.assert_not_called()

    def test_credentials_are_not_persisted_even_in_client_exception(self) -> None:
        self.write_samples([labeled()])
        client = Mock(side_effect=RuntimeError(f"{CONFIG.api_url} {CONFIG.api_key}"))
        code, _ = self.run_samples(client)
        self.assertEqual(code, 2)
        for artifact in self.output.iterdir():
            text = self.artifact_text(artifact)
            self.assertNotIn(CONFIG.api_url, text)
            self.assertNotIn(CONFIG.api_key, text)

    def test_keyboard_interrupt_preserves_finished_results(self) -> None:
        self.write_samples([labeled(), labeled()])
        client = Mock(side_effect=[(json.dumps(PASSED), 1.0, 2.0), KeyboardInterrupt()])
        code, _ = self.run_samples(client)
        self.assertEqual(code, 130)
        self.assertEqual(len(self.read_results()), 1)
        info = self.read_info()
        self.assertEqual(info["status"], "interrupted")
        self.assertEqual(info["counts"], {"selected": 2, "processed": 1, "passed": 1, "rejected": 0, "failed": 0})

    def test_required_paths_and_invalid_cli_limit_do_not_load_model(self) -> None:
        arguments = [[], ["--input", str(self.input)], ["--output-dir", str(self.output)],
                     ["--input", str(self.input), "--output-dir", str(self.output), "--limit", "0"]]
        for argv in arguments:
            with self.subTest(argv=argv), patch.object(runner, "load_llm_config") as load:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    runner.main(argv)
                self.assertEqual(raised.exception.code, 2)
                load.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_actual_client_with_mock_http_and_relative_paths(self) -> None:
        self.write_samples([labeled(), labeled()])
        config_path = self.root / "model.json"
        config_path.write_text(json.dumps({
            "api_url": CONFIG.api_url, "api_key": CONFIG.api_key,
            "model": CONFIG.model, **CONFIG.options(),
        }), encoding="utf-8")
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(PASSED)}}]}
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch("requests.post", return_value=response) as post, contextlib.redirect_stdout(io.StringIO()):
                code = runner.main(["--input", "samples.jsonl", "--output-dir", "relative_output",
                                    "--prompt", "prompt.md", "--llm-config", "model.json", "--limit", "1"])
        finally:
            os.chdir(previous_cwd)
        self.assertEqual(code, 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["model"], CONFIG.model)
        self.assertIn('"conversation_turns"', post.call_args.kwargs["json"]["messages"][0]["content"])
        self.output = self.root / "relative_output"
        self.assertEqual(self.read_info()["input_path"], str(self.input.resolve()))
        self.assertEqual(len(self.read_results()), 1)
        for artifact in self.output.iterdir():
            self.assertNotIn(CONFIG.api_key, self.artifact_text(artifact))

    def test_xlsx_literal_text_long_values_and_original_fields(self) -> None:
        source = labeled("=1+1")
        source.update({
            "L2": "#REF!", "session_id": "0001234567890123456789",
            "cluster_id": 1234567890123456789, "extra_control": "A\x01B",
            "extra_long": "x" * 40000, "extra_nested": {"key": ["值"]},
            "status": "old-status", "source.status": "another-status",
        })
        self.write_samples([source, source])
        code, client = self.run_samples()
        self.assertEqual(code, 0)
        self.assertEqual(client.call_count, 2)
        workbook = load_workbook(self.output / "results.xlsx")
        try:
            sheet = workbook["Stage1结果"]
            self.assertEqual(workbook.sheetnames, ["Stage1结果"])
            rows = list(sheet.values)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(rows[0]), len(set(rows[0])))
            record = dict(zip(rows[0], rows[1]))
            self.assertEqual(record["target_query"], "=1+1")
            self.assertEqual(record["L2"], "#REF!")
            self.assertEqual(record["session_id"], source["session_id"])
            self.assertEqual(record["cluster_id"], str(source["cluster_id"]))
            self.assertEqual(record["extra_control"], "A\\u0001B")
            self.assertEqual(len(record["extra_long"]), 32767)
            self.assertIn("完整内容见 results.jsonl", record["extra_long"])
            self.assertEqual(json.loads(record["extra_nested"]), source["extra_nested"])
            self.assertEqual(record["source.status"], "old-status")
            self.assertEqual(record["source.source.status"], "another-status")
            self.assertEqual(json.loads(record["stage1_input"]), self.read_results()[0]["stage1_input"])
            self.assertFalse(any(cell.data_type in {"f", "e"} for row in sheet for cell in row))
        finally:
            workbook.close()
        self.assertEqual(self.read_results()[0]["source_record"], source)

    def test_xlsx_dependency_checked_before_model_call(self) -> None:
        self.write_samples([labeled()])
        client = Mock()
        with patch.object(runner, "validate_xlsx_dependency", side_effect=RuntimeError("missing dependency")):
            with self.assertRaises(RuntimeError):
                self.run_samples(client)
        client.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_xlsx_export_failure_keeps_json_results(self) -> None:
        self.write_samples([labeled()])
        client = Mock(return_value=(json.dumps(PASSED), 1.0, 2.0))
        with patch.object(runner, "write_results_xlsx", side_effect=OSError("export failed")):
            with self.assertRaises(OSError):
                self.run_samples(client)
        client.assert_called_once()
        self.assertTrue(self.read_results()[0]["stage1_pass"])
        self.assertTrue((self.output / "report.md").is_file())

    def test_default_prompt_and_config_are_independent_of_cwd(self) -> None:
        self.write_samples([labeled()])
        client = Mock(return_value=(json.dumps(PASSED), 1.0, 2.0))
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(runner, "load_llm_config", return_value=CONFIG) as load:
                with patch.object(LlmConfig, "bind", return_value=client), contextlib.redirect_stdout(io.StringIO()):
                    code = runner.main(["--input", "samples.jsonl", "--output-dir", "output"])
        finally:
            os.chdir(previous_cwd)
        self.assertEqual(code, 0)
        load.assert_called_once_with(runner.DEFAULT_LLM_CONFIG_PATH)
        self.assertEqual((self.output / "stage1_prompt.md").read_text(encoding="utf-8"), runner.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(client.call_count, 1)

    def test_direct_script_and_module_entrypoints(self) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        script = Path(runner.__file__).resolve()
        commands = [
            ([sys.executable, "-B", str(script), "--help"], self.root, 0),
            ([sys.executable, "-B", str(script), "--input", str(self.input)], self.root, 2),
            ([sys.executable, "-B", "-m", "screening.module_tests.run_stage1", "--help"], script.parents[2], 0),
        ]
        for command, cwd, expected in commands:
            with self.subTest(command=command):
                completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, encoding="utf-8", timeout=30)
                self.assertEqual(completed.returncode, expected, completed.stderr)
                self.assertIn("--output-dir", completed.stdout + completed.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
