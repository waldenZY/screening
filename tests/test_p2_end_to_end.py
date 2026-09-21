"""Small mocked-LLM integration check for the three-stage pipeline."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from screening.candidate.index import ensure_canonical_candidate_index
from screening.candidate.sampling import P1SamplingConfig, P2SamplingConfig, SamplingConfig
from screening.delivery.publish import collect_and_validate_artifacts
from screening.pipeline.orchestrator import (
    BuildConfig,
    P1SupplementConfig,
    P2SupplementConfig,
    run_build,
)
from screening.quality.two_stage import (
    QualityRunConfig,
    QualityStageIncomplete,
    TwoStageQualityRunner,
)
from screening.shared.json_io import sha256_file
from screening.shared.taxonomy import load_taxonomy_mappings


class P2EndToEndTests(unittest.TestCase):
    def test_official_p2_is_challenge_gated_and_publishable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "clusters.jsonl"
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(json.dumps({
                "categories": [{
                    "name": "account",
                    "total_size": 12,
                    "assigned_clusters": [1, 2],
                    "sub_intents": [{
                        "name": "balance",
                        "size": 12,
                        "cluster_ids": [1, 2],
                    }],
                }],
            }), encoding="utf-8")
            with source.open("w", encoding="utf-8") as handle:
                for number in range(12):
                    token = hashlib.sha256(str(number).encode()).hexdigest()[:10]
                    handle.write(json.dumps({
                        "cluster": 1 if number < 6 else 2,
                        "session": f"s-{number}",
                        "text": f"balance unusual expression {token}",
                        "cluster_prob": 0.9,
                        "frequency": number + 1,
                    }) + "\n")
            taxonomy = load_taxonomy_mappings(taxonomy_path)
            index = ensure_canonical_candidate_index(
                source,
                taxonomy,
                taxonomy_sha256=sha256_file(taxonomy_path),
                cache_root=root / "cache",
                min_free_bytes=0,
                reserve_free_bytes=0,
            )
            calls = {"S1": 0, "CH": 0, "S2": 0}

            def llm_call(messages, **_kwargs):
                prompt = messages[0]["content"]
                stage = prompt[:2]
                calls[stage] += 1
                if stage == "S1":
                    value = {
                        "category_fit": "exact",
                        "semantic_completeness": "self_contained",
                        "intent_clarity": "clear",
                        "validity": "valid",
                    }
                elif stage == "CH":
                    if calls[stage] == 1:
                        return "invalid-json", 1.0, 2.0
                    value = {
                        "is_challenge": True,
                        "primary_challenge": "long_tail_expr",
                        "challenge_types": ["long_tail_expr"],
                    }
                else:
                    value = {
                        "classification_difficulty": "easy",
                        "task_difficulty": "simple",
                        "issue_types": ["none"],
                        "overall_score": 5,
                    }
                return json.dumps(value), 1.0, 2.0

            run_root = root / "run"
            runner = TwoStageQualityRunner(
                run_root / "quality",
                stage1_prompt="S1 {CANDIDATE_JSON}",
                challenge_prompt="CH {CANDIDATE_JSON}",
                stage2_prompt="S2 {CANDIDATE_JSON}",
                model="fake",
                llm_options={
                    "max_tokens": 100,
                    "stream": False,
                    "enable_thinking": False,
                    "skip_special_tokens": True,
                    "timeout": 1.0,
                },
                llm_call=llm_call,
                config=QualityRunConfig(workers=2, sleep_seconds=0, max_retries=1),
            )
            config = BuildConfig(
                sampling=SamplingConfig(
                    p1=P1SamplingConfig(target_n=2),
                    p2=P2SamplingConfig(
                        target_n=2,
                        soft_min_per_type=0,
                        max_per_primary=2,
                        batch_size=2,
                    ),
                ),
                p1_supplement=P1SupplementConfig(max_rounds=0),
                p2_supplement=P2SupplementConfig(
                    max_rounds=0,
                    default_yield=0.9,
                    type_default_yield=0.1,
                ),
            )
            with self.assertRaises(QualityStageIncomplete):
                run_build(
                    index,
                    config,
                    source_path=source,
                    taxonomy=taxonomy,
                    run_root=run_root,
                    quality_runner=runner,
                )
            stage1_calls_before_resume = calls["S1"]
            outcome = run_build(
                index,
                config,
                source_path=source,
                taxonomy=taxonomy,
                run_root=run_root,
                quality_runner=runner,
            )
            self.assertEqual(calls["S1"], stage1_calls_before_resume)
            self.assertEqual(len(outcome.records["P2"]), 2)
            self.assertTrue(all(record["challenge"]["is_challenge"] for record in outcome.records["P2"]))
            contract = json.loads((run_root / "p012_run_contract.json").read_text(encoding="utf-8"))
            self.assertEqual(contract["p2_supplement_algorithm"], "adaptive_challenge_batches_1")
            funnel = outcome.summary["p2_adaptive"]["source_funnel"]
            self.assertEqual(
                sum(row["actual_sampled"] for row in funnel.values()),
                sum(batch["sampled"] for wave in outcome.summary["p2_adaptive"]["rounds"] for batch in wave["batches"]),
            )
            artifacts = collect_and_validate_artifacts(run_root)
            self.assertIn("challenge_tasks.jsonl", artifacts["quality"])
            final_ids = {
                record["candidate"]["source_identity_sha256"]
                for values in outcome.records.values()
                for record in values
            }
            stage2_tasks = [
                json.loads(line)
                for line in (run_root / "quality" / "stage2_tasks.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                {identity for task in stage2_tasks for identity in task["source_identities"]},
                final_ids,
            )


if __name__ == "__main__":
    unittest.main()
