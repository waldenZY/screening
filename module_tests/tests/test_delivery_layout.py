"""Delivery completeness, immutable Stage1 reuse and failure recovery with fake LLMs."""
import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screening.pipeline.cli import main
from screening.module_tests.tests import test_downstream as fixtures
from screening.module_tests.tests.test_run_admission import CONFIG, fake_client
from screening.delivery.labeled import LABELED_FILENAMES, base_filename, base_fields, derived_quality
from screening.delivery.package import SLICES
from screening.delivery.validation import collect_and_validate_artifacts
from screening.shared.json_io import load_jsonl, sha256_file


class DeliveryLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = fixtures.StageBoundaryTests()._profile(self.root)
        self.calls = []
        self.client = fake_client(self.calls)

    def invoke(self, *args, client=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.profile), *map(str, args)], llm_config_override=CONFIG,
                        llm_call_override=client or self.client)
        return code, out.getvalue() + err.getvalue()

    def runs(self):
        return sorted((self.root / "outputs").glob("*/internal"))

    def create(self):
        code, log = self.invoke()
        self.assertEqual(code, 0, log[-3000:])
        return self.runs()[-1]

    def test_files_alignment_reports_slices_and_no_duplicate_exports(self):
        internal = self.create()
        package = internal.parent / "external/evaluation-set-large"
        manifest = json.loads((internal / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["delivery"]["status"], "completed")
        self.assertFalse((self.root / "outputs/runs").exists())
        totals = 0
        for name in LABELED_FILENAMES:
            bases, labeled = load_jsonl(package / base_filename(name)), load_jsonl(package / LABELED_FILENAMES[name])
            canonical = load_jsonl(internal / name / "screened.canonical.jsonl")
            totals += len(bases)
            self.assertEqual(bases, [{k: row[k] for k in base_fields(name)} for row in labeled])
            self.assertEqual(len(bases), len(canonical))
            for row, record in zip(labeled, canonical):
                self.assertEqual(len(row), 31 if name == "P2" else 30)
                self.assertNotIn("stage1", row)
                self.assertNotIn("provenance", row)
                for key, value in derived_quality(record).items():
                    self.assertEqual(row[key], value)
                self.assertEqual(row["quality_overall_score"], record["downstream_quality"]["quality_overall_score"])
            for filename, predicate in SLICES.items():
                self.assertEqual(load_jsonl(package / "质量标注/subsets" / f"{name}-质量分层子集" / filename),
                                 [r for r in labeled if predicate(r)])
            self.assertFalse(list((internal / name).glob("*_labeled.jsonl")))
            self.assertFalse((internal / name / "branch_audit.json").exists())
            self.assertFalse((internal / name / "stage1_freeze_report.json").exists())
        report = json.loads((package / "sampling_report_v4.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(report[n]["total"] for n in LABELED_FILENAMES), totals)
        self.assertEqual(sum(report["all_tags"]["prob"].values()), totals)
        self.assertEqual(list((package / "可视化").iterdir()), [])
        self.assertEqual(len(list(package.rglob("*.jsonl"))), 36)
        collect_and_validate_artifacts(internal)

    def test_all_mapping_boundaries_and_independent_quality_score(self):
        internal = self.create()
        record = load_jsonl(internal / "P1/screened.canonical.jsonl")[0]
        for band, expected in (("high", "trivial"), ("mid", "moderate"), ("boundary", "hard")):
            for route, semantic in (("direct", "self_contained"), ("clarification", "context_dependent")):
                for complexity in ("simple", "moderate", "complex"):
                    sample = copy.deepcopy(record)
                    sample["candidate"]["features"].update(probability_band=band, complexity=complexity)
                    sample["quality"]["admission"]["screening"]["route"] = route
                    value = derived_quality(sample)
                    self.assertEqual(value["quality_classification_difficulty"], expected)
                    self.assertEqual(value["quality_semantic_completeness"], semantic)
                    self.assertEqual(value["quality_task_difficulty"], complexity)
        wrong = copy.deepcopy(record)
        wrong["candidate"]["features"].pop("complexity")
        with self.assertRaises((KeyError, ValueError)):
            derived_quality(wrong)

    def test_frozen_failure_resume_and_completed_reuse(self):
        def fail(messages, **kwargs):
            if messages[0]["content"].startswith("# 下游质量补充标注"):
                raise RuntimeError("synthetic failure")
            return self.client(messages, **kwargs)
        code, _ = self.invoke(client=fail)
        self.assertEqual(code, 2)
        internal = self.runs()[0]
        frozen = {n: (internal / n / "stage1.frozen.jsonl").read_bytes() for n in LABELED_FILENAMES}
        before = len(self.calls)
        with patch("screening.pipeline.orchestrator._sample_branch", side_effect=AssertionError("must not resample")), patch("screening.pipeline.orchestrator._run_adaptive_p2", side_effect=AssertionError("must not rebuild P2")):
            code, log = self.invoke("--run-id", internal.parent.name)
        self.assertEqual(code, 0, log[-3000:])
        self.assertTrue(all(not p.startswith(("# 准入评估", "# P2")) for p in self.calls[before:]))
        self.assertEqual(frozen, {n: (internal / n / "stage1.frozen.jsonl").read_bytes() for n in LABELED_FILENAMES})
        before = len(self.calls)
        code, log = self.invoke("--run-id", internal.parent.name)
        self.assertEqual(code, 0, log[-3000:])
        self.assertEqual(len(self.calls), before + 1)  # connectivity only

    def test_stage1_from_new_layout_reuses_annotations_and_keeps_source_immutable(self):
        source = self.create()
        hashes = {str(p.relative_to(source)): sha256_file(p) for p in source.rglob("*") if p.is_file()}
        before = len(self.calls)
        with patch("screening.pipeline.orchestrator._sample_branch", side_effect=AssertionError("must not resample")), patch("screening.pipeline.orchestrator._run_adaptive_p2", side_effect=AssertionError("must not rebuild P2")):
            code, log = self.invoke("--stage1-from", source.parent)
        self.assertEqual(code, 0, log[-3000:])
        target = self.runs()[-1]
        self.assertNotEqual(source, target)
        self.assertEqual(len(self.calls), before + 1)
        for n in LABELED_FILENAMES:
            self.assertEqual((source / n / "stage1.frozen.jsonl").read_bytes(), (target / n / "stage1.frozen.jsonl").read_bytes())
        self.assertEqual(hashes, {str(p.relative_to(source)): sha256_file(p) for p in source.rglob("*") if p.is_file()})
        collect_and_validate_artifacts(target)
        code, log = self.invoke("--run-id", target.parent.name)
        self.assertEqual(code, 0, log[-3000:])

    def test_stage1_from_old_layout_and_changed_annotation_prompt(self):
        source = self.create()
        legacy = self.root / "legacy-v4"
        shutil.copytree(source, legacy)
        manifest = json.loads((legacy / "manifest.json").read_text(encoding="utf-8"))
        manifest.update(pipeline_version=9, manifest_schema_version=9)
        manifest["effective_config"] = json.loads((source / "snapshots/config.json").read_text(encoding="utf-8"))["effective_config"]
        (legacy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        shutil.copytree(source / "snapshots/prompts", legacy / "prompts")
        shutil.copyfile(source / "snapshots/quality_contract.json", legacy / "quality/quality_run_contract.json")
        for filename in ("risk_keywords.json", "dedup_idf.json"):
            shutil.copyfile(source / "snapshots" / filename, legacy / filename)
        for n in LABELED_FILENAMES:
            (legacy / n / "stage1_freeze_report.json").write_text(json.dumps(manifest["frozen"][n]), encoding="utf-8")
        prompt = self.root / "changed.md"
        prompt.write_text((source / "snapshots/prompts/quality_annotation.md").read_text(encoding="utf-8") + "\n", encoding="utf-8")
        before = len(self.calls)
        code, log = self.invoke("--stage1-from", legacy, "--quality-annotation-prompt", prompt)
        self.assertEqual(code, 0, log[-3000:])
        self.assertGreater(len(self.calls), before + 1)
        self.assertTrue(all(not p.startswith("# 准入评估") for p in self.calls[before:]))

    def test_changed_model_rejudges_only_four_fields_and_resumes_without_workbook(self):
        from dataclasses import replace
        source = self.create()
        (self.root / "risk.xlsx").unlink()
        calls = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--config", str(self.profile), "--stage1-from", str(source.parent)],
                        llm_config_override=replace(CONFIG, model="another-offline-model"),
                        llm_call_override=fake_client(calls))
        self.assertEqual(code, 0, out.getvalue()[-3000:])
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(not p.startswith("# 准入评估") for p in calls))
        target = self.runs()[-1]
        collect_and_validate_artifacts(target)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--config", str(self.profile), "--run-id", target.parent.name],
                        llm_config_override=replace(CONFIG, model="another-offline-model"),
                        llm_call_override=fake_client(calls))
        self.assertEqual(code, 0, out.getvalue()[-3000:])

    def test_source_annotation_tampering_is_not_imported(self):
        source = self.create()
        path = source / "quality/quality_annotation_results.jsonl"
        rows = load_jsonl(path)
        rows[0]["result"] = {"quality_validity":"valid", "quality_intent_clarity":"clear",
                             "quality_overall_score":5,"quality_issue_types":["none"]}
        # An otherwise valid result is still rejected if its recorded file hash changes.
        path.write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")
        before = len(self.calls)
        code, _ = self.invoke("--stage1-from", source.parent)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), before + 1)  # preflight, never sample/model annotation

    def test_manifest_parallel_readers_and_updates_keep_all_branch_fields(self):
        from concurrent.futures import ThreadPoolExecutor
        from screening.pipeline.run_context import _load_manifest, update_manifest
        folder = self.root / "concurrent"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"branches":{}}), encoding="utf-8")
        def writer(branch):
            for n in range(25):
                update_manifest(folder, branches={branch:{"counter":n}})
        def reader():
            for _ in range(100):
                self.assertIsInstance(_load_manifest(folder / "manifest.json")["branches"], dict)
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = [pool.submit(writer, n) for n in ("P0","P1","P2")] + [pool.submit(reader) for _ in range(3)]
            for job in jobs:
                job.result()
        self.assertEqual(_load_manifest(folder / "manifest.json")["branches"], {n:{"counter":24} for n in ("P0","P1","P2")})

    def test_missing_and_tampered_files_rejected(self):
        source = self.create()
        package = source.parent / "external/evaluation-set-large"
        for path in (package / base_filename("P0"), package / "sampling_report_v4.json", source / "P1/stage1.frozen.jsonl"):
            original = path.read_bytes()
            path.write_bytes(b"{}\n")
            with self.assertRaises((ValueError, KeyError, OSError, TypeError)):
                collect_and_validate_artifacts(source)
            path.write_bytes(original)
        path = source / "P1/stage1.frozen.jsonl"
        original = path.read_bytes()
        path.write_bytes(b"")
        before = len(self.calls)
        code, _ = self.invoke("--stage1-from", source.parent)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls), before)
        path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
