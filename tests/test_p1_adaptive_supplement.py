"""Focused checks for adaptive P1 supplementation and its report."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from screening.candidate.index import ensure_canonical_candidate_index
from screening.candidate.sampling import (
    P1SamplingConfig,
    P2SamplingConfig,
    SamplingConfig,
    allocate_feedback_cluster_quotas,
)
from screening.delivery.report import load_and_validate_run_report
from screening.pipeline.orchestrator import BuildConfig, P1SupplementConfig, run_build
from screening.shared.json_io import sha256_file
from screening.shared.taxonomy import load_taxonomy_mappings


REJECT = {
    "category_fit": "mismatch",
    "semantic_completeness": "self_contained",
    "intent_clarity": "clear",
    "validity": "valid",
}
PASS = {**REJECT, "category_fit": "exact"}
STAGE2 = {
    "classification_difficulty": "easy",
    "task_difficulty": "simple",
    "issue_types": ["none"],
    "overall_score": 5,
}


class FakeQualityRunner:
    def __init__(self, *, fail_once_at: int | None = None) -> None:
        self.stage1_calls = 0
        self.fail_once_at = fail_once_at

    def run_stage1(self, candidates):
        self.stage1_calls += 1
        if self.stage1_calls == self.fail_once_at:
            raise RuntimeError("simulated Stage1 interruption")
        label = PASS if self.stage1_calls >= 3 else REJECT
        return {item["source_identity_sha256"]: dict(label) for item in candidates}

    def run_stage2(self, candidates, _stage1):
        return {item["source_identity_sha256"]: dict(STAGE2) for item in candidates}


class AdaptiveP1Tests(unittest.TestCase):
    def test_cluster_feedback_and_once_per_round_exploration(self):
        quotas = allocate_feedback_cluster_quotas(
            {"a": 400, "b": 100}, 10,
            evaluated_passed={"a": (30, 0), "b": (10, 6)},
            l2_prior_rate=0.2, prior_strength=10,
            round_draws={}, explore_all_clusters=True,
        )
        self.assertEqual(sum(quotas.values()), 10)
        self.assertGreater(quotas["b"], quotas["a"])
        self.assertEqual(
            allocate_feedback_cluster_quotas(
                {"a": 400, "b": 100}, 1,
                evaluated_passed={}, l2_prior_rate=0.2,
                prior_strength=10, round_draws={"a": 1}, explore_all_clusters=True,
            ),
            {"b": 1},
        )
        self.assertEqual(
            allocate_feedback_cluster_quotas(
                {"a": 400, "b": 100}, 1,
                evaluated_passed={}, l2_prior_rate=0.2,
                prior_strength=10, round_draws={"a": 1}, explore_all_clusters=False,
            ),
            {"a": 1},
        )

    def test_multi_batch_build_report_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clusters.jsonl"
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(json.dumps({
                "categories": [{
                    "name": "账户", "total_size": 40,
                    "assigned_clusters": [1, 2],
                    "sub_intents": [{"name": "查询余额", "size": 40,
                                     "cluster_ids": [1, 2]}],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            with source.open("w", encoding="utf-8") as handle:
                for number in range(40):
                    token = hashlib.sha256(str(number).encode()).hexdigest()[:12]
                    row = {
                        "cluster": 1 if number < 20 else 2,
                        "session": f"session-{number}",
                        "text": f"查询余额编号{token}",
                        "cluster_prob": 0.95,
                        "frequency": 1,
                    }
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            taxonomy = load_taxonomy_mappings(taxonomy_path)
            index = ensure_canonical_candidate_index(
                source, taxonomy, taxonomy_sha256=sha256_file(taxonomy_path),
                cache_root=root / "cache", min_free_bytes=0, reserve_free_bytes=0,
            )
            config = BuildConfig(
                sampling=SamplingConfig(
                    p1=P1SamplingConfig(target_n=2),
                    p2=P2SamplingConfig(target_n=1),
                ),
                p1_supplement=P1SupplementConfig(max_rounds=2),
            )
            runner = FakeQualityRunner(fail_once_at=3)
            run_root = root / "run"
            with self.assertRaisesRegex(RuntimeError, "simulated Stage1 interruption"):
                run_build(
                    index, config, source_path=source, taxonomy=taxonomy,
                    run_root=run_root, quality_runner=runner,
                )
            first = run_build(
                index, config, source_path=source, taxonomy=taxonomy,
                run_root=run_root, quality_runner=runner,
            )
            supplement = first.summary["p1_supplement"]
            self.assertEqual(first.summary["sets"]["P1"]["final"], 2)
            self.assertEqual(supplement["rounds"][0]["admitted"], 2)
            self.assertGreaterEqual(len(supplement["rounds"][0]["batches"]), 2)
            self.assertEqual(first.summary["sets"]["P1"]["supplement_pass_not_admitted"], 0)
            report = load_and_validate_run_report(run_root)
            self.assertEqual(report["report_schema_version"], 3)
            self.assertEqual(report["p1_diagnostics"]["rounds"][0]["admitted"], 2)
            replay = run_build(
                index, config, source_path=source, taxonomy=taxonomy,
                run_root=run_root, quality_runner=runner,
            )
            self.assertEqual(replay.summary, first.summary)
            self.assertEqual(runner.stage1_calls, 4)


if __name__ == "__main__":
    unittest.main()
