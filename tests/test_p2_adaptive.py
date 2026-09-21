"""Contract and allocation checks for adaptive P2."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from screening.candidate.index import CanonicalCandidateIndex, ensure_canonical_candidate_index
from screening.candidate.p2_adaptive import (
    AdaptiveP2Sampler,
    allocate_by_weights,
    select_final_p2,
    source_yield_rates,
    source_tags,
)
from screening.candidate.sampling import P2SamplingConfig
from screening.delivery.publish import PublicationError, publish_labeled
from screening.quality.two_stage import (
    CHALLENGE_TYPES,
    QualityResponseError,
    QualityRunConfig,
    TwoStageQualityRunner,
    validate_challenge_result,
)
from screening.shared.json_io import sha256_file
from screening.shared.taxonomy import load_taxonomy_mappings


def candidate(number: int, challenge_type: str) -> dict:
    return {
        "source_identity_sha256": f"{number:064x}",
        "conversation": {
            "sample_type": "single_turn",
            "context_turns": [],
            "target_query": f"query-{challenge_type}-{number}",
            "full_text": f"query-{challenge_type}-{number}",
        },
        "taxonomy": {
            "l1": "root",
            "l2": f"leaf-{number % 4}",
            "l2_key": f"root/leaf-{number % 4}",
            "cluster_id": number % 6,
        },
        "source": {"source_line": number + 1},
    }


class P2AdaptiveTests(unittest.TestCase):
    def test_challenge_contract(self):
        self.assertEqual(
            validate_challenge_result({
                "is_challenge": False,
                "primary_challenge": None,
                "challenge_types": [],
            })["challenge_types"],
            [],
        )
        valid = {
            "is_challenge": True,
            "primary_challenge": "ambiguity",
            "challenge_types": ["ambiguity", "long_tail_expr", "boundary"],
        }
        self.assertEqual(validate_challenge_result(valid), valid)
        invalid_values = [
            {**valid, "primary_challenge": None},
            {**valid, "challenge_types": ["boundary", "ambiguity"]},
            {**valid, "challenge_types": ["ambiguity", "ambiguity"]},
            {"is_challenge": False, "primary_challenge": "boundary", "challenge_types": []},
        ]
        for value in invalid_values:
            with self.assertRaises(QualityResponseError):
                validate_challenge_result(value)

    def test_source_tags_and_integer_share_allocation(self):
        config = P2SamplingConfig()
        locator = {
            "sample_type": "multi_turn",
            "cluster_probability": 0.49,
            "features": {"complexity": "complex"},
        }
        self.assertEqual(source_tags(locator, config), ("complex", "multi_turn", "boundary"))
        locator = {
            "sample_type": "single_turn",
            "cluster_probability": 0.5,
            "features": {"complexity": "simple"},
        }
        self.assertEqual(source_tags(locator, config), ("exploration",))
        allocation = allocate_by_weights(10, config.candidate_source_shares)
        self.assertEqual(allocation, {"complex": 3, "multi_turn": 3, "boundary": 2, "exploration": 2})

    def test_p2_configuration_rejects_an_impossible_total_cap(self):
        with self.assertRaisesRegex(ValueError, "上限之和"):
            P2SamplingConfig(target_n=801, soft_min_per_type=0, max_per_primary=100)

    def test_publish_gate_requires_p2_stages(self):
        completed = {
            name: {"status": "completed"}
            for name in (
                "canonical_index", "p012_build", "quality_stage1", "p1_supplement",
                "quality_stage2", "screened_export", "legacy_export", "batch_report",
            )
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(PublicationError, "p2_supplement"):
                publish_labeled(
                    root / "run",
                    root / "published",
                    {"pipeline_version": 3, "stages": completed},
                )

    def test_source_yield_uses_all_sampled_candidates_as_denominator(self):
        metadata = {
            "kept": {"sampled_from": "complex"},
            "deduplicated": {"sampled_from": "complex"},
        }
        challenge = {
            "kept": {
                "is_challenge": True,
                "primary_challenge": "boundary",
                "challenge_types": ["boundary"],
            }
        }
        total, typed = source_yield_rates(
            metadata,
            challenge,
            default_yield=0.1,
            type_default_yield=0.01,
            prior_strength=10.0,
            minimum_yield=0.001,
        )
        self.assertAlmostEqual(total["complex"], 2.0 / 12.0)
        self.assertAlmostEqual(typed["complex"]["boundary"], 1.1 / 12.0)

    def test_overlapping_source_pools_refill_and_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(json.dumps({
                "categories": [{
                    "name": "account",
                    "total_size": 13,
                    "assigned_clusters": [1],
                    "sub_intents": [{"name": "balance", "size": 13, "cluster_ids": [1]}],
                }],
            }), encoding="utf-8")
            rows = []
            for number in range(3):
                rows.append((f"请帮我查询余额并说明最近交易的具体变化原因{number}？", 0.9))
                rows.append((f"你好{number}呀 | 查余额", 0.9))
                rows.append((f"余额{number}", 0.4))
                rows.append((f"查余额{number}", 0.5))
            rows.append(("请先查询余额然后核对账单并告诉我异常原因？ | 再看看限额", 0.4))
            source = root / "clusters.jsonl"
            with source.open("w", encoding="utf-8") as handle:
                for number, (text_value, probability) in enumerate(rows):
                    handle.write(json.dumps({
                        "cluster": 1,
                        "session": f"session-{number}",
                        "text": text_value,
                        "cluster_prob": probability,
                        "frequency": number + 1,
                    }, ensure_ascii=False) + "\n")
            taxonomy = load_taxonomy_mappings(taxonomy_path)
            index = ensure_canonical_candidate_index(
                source,
                taxonomy,
                taxonomy_sha256=sha256_file(taxonomy_path),
                cache_root=root / "cache",
                min_free_bytes=0,
                reserve_free_bytes=0,
            )
            request = {"complex": 2, "multi_turn": 2, "boundary": 2, "exploration": 2}
            draws = []
            for _ in range(2):
                with CanonicalCandidateIndex(index) as candidate_index:
                    sampler = AdaptiveP2Sampler(candidate_index, P2SamplingConfig())
                    sampler.capacities()
                    draw = sampler.draw(request, ())
                    identities = [value["source_identity_sha256"] for value in draw.locators]
                    self.assertEqual(len(identities), 8)
                    self.assertEqual(len(set(identities)), 8)
                    self.assertEqual(
                        {source: sum(
                            value["sampled_from"] == source for value in draw.metadata.values()
                        ) for source in request},
                        request,
                    )
                    draws.append(identities)
            self.assertEqual(draws[0], draws[1])

    def test_soft_floor_cap_and_scarce_type_replacement(self):
        config = P2SamplingConfig(
            target_n=16,
            soft_min_per_type=2,
            max_per_primary=4,
            batch_size=8,
        )
        candidates = []
        results = {}
        number = 1
        for challenge_type in CHALLENGE_TYPES:
            amount = 1 if challenge_type == "cross_app" else 6
            for _ in range(amount):
                value = candidate(number, challenge_type)
                candidates.append(value)
                results[value["source_identity_sha256"]] = {
                    "is_challenge": True,
                    "primary_challenge": challenge_type,
                    "challenge_types": [challenge_type],
                }
                number += 1
        selected, diagnostics = select_final_p2(candidates, results, config)
        self.assertEqual(len(selected), 16)
        self.assertEqual(diagnostics["soft_min_deficit_by_primary"]["cross_app"], 1)
        self.assertTrue(all(value <= 4 for value in diagnostics["selected_by_primary"].values()))

        extra = candidate(number, "cross_app")
        candidates.append(extra)
        results[extra["source_identity_sha256"]] = {
            "is_challenge": True,
            "primary_challenge": "cross_app",
            "challenge_types": ["cross_app"],
        }
        replaced, diagnostics = select_final_p2(candidates, results, config)
        self.assertEqual(len(replaced), 16)
        self.assertEqual(diagnostics["soft_min_deficit_by_primary"]["cross_app"], 0)
        self.assertEqual(diagnostics["selected_by_primary"]["cross_app"], 2)

    def test_challenge_retry_cache_and_ledgers(self):
        value = candidate(999, "boundary")
        value["conversation_sha256"] = "a" * 64
        calls = []

        def llm_call(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return "not-json", 1.0, 2.0
            return json.dumps({
                "is_challenge": True,
                "primary_challenge": "boundary",
                "challenge_types": ["boundary"],
            }), 1.0, 2.0

        with tempfile.TemporaryDirectory() as temporary:
            runner = TwoStageQualityRunner(
                Path(temporary),
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
                config=QualityRunConfig(workers=1, sleep_seconds=0, max_retries=2),
            )
            first = runner.run_challenge([value])
            second = runner.run_challenge([value])
            third = runner.run_challenge([value])
            self.assertEqual(first, second)
            self.assertEqual(second, third)
            self.assertEqual(len(calls), 2)
            attempts = (Path(temporary) / "challenge_attempts.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(attempts), 2)
            cache_hits = (Path(temporary) / "challenge_cache_hits.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(cache_hits), 1)
            self.assertTrue((Path(temporary) / "challenge_tasks.jsonl").is_file())
            self.assertTrue((Path(temporary) / "challenge_results.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
