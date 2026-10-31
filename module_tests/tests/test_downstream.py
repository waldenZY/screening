"""Offline regression tests for phase boundaries and the downstream contract."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from screening.candidate.contract import compute_fixed_features, parse_conversation, _formality
from screening.candidate.scoring import sampling_scores
from screening.delivery.publish import collect_and_validate_artifacts, PublicationError
from screening.delivery.labeled import labeled_fields
from screening.quality.annotation import QUALITY_FIELDS, ISSUE_TYPES, validate_quality_annotation, quality_annotation_input
from screening.quality.admission import QualityResponseError, build_admission_result
from screening.quality.runner import QualityRunner, QualityRunConfig, QualityStageIncomplete
from screening.pipeline.cli import main as pipeline_main
from screening.module_tests.tests.test_run_admission import CONFIG, PACKAGE, CATEGORY, feature, fake_client


def annotation(score=5):
    return {
        "quality_validity": "valid" if score >= 4 else "borderline" if score == 3 else "invalid",
        "quality_intent_clarity": "no_intent" if score == 1 else "clear",
        "quality_overall_score": score,
        "quality_issue_types": ["none"] if score == 5 else ["no_evaluable_goal"] if score == 1 else ["context_dependent"],
    }


class DownstreamQualityTests(unittest.TestCase):
    def test_annotation_retry_cache_and_complete_admission_input_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            calls=[]
            def client(messages, **kwargs):
                calls.append(messages[0]["content"])
                # First syntactically valid response violates the quality contract.
                result = {**annotation(), "quality_validity":"borderline"} if len(calls)==1 else annotation()
                return json.dumps(result),1.0,2.0
            kwargs=dict(admission_prompt=(PACKAGE/"artifacts/prompts/quality_admission.md").read_text(encoding="utf-8"),
                challenge_prompt=(PACKAGE/"artifacts/prompts/quality_challenge.md").read_text(encoding="utf-8"),
                model=CONFIG.model,llm_options=CONFIG.options(),llm_call=client,
                config=QualityRunConfig(workers=2,sleep_seconds=0,max_retries=2))
            a={"source_identity_sha256":"a"*64,"conversation_sha256":"c"*64,
               "conversation":parse_conversation("帮我设闹钟"),"source":{"source_line":1},
               "taxonomy":{"l1":"设备","l2":"闹钟"}}
            b=copy.deepcopy(a);b["source_identity_sha256"]="b"*64;b["source"]["source_line"]=2
            admitted=build_admission_result(feature(["帮我设闹钟"],points=("parameter",)),CATEGORY)
            runner=QualityRunner(Path(td),**kwargs);runner.bind_admission({"a"*64:admitted,"b"*64:admitted})
            with patch("screening.quality.runner.time.sleep"):
                results=runner.run_additional_stage("quality_annotation",[a,b])
            self.assertEqual(len(calls),2)
            self.assertEqual(results["a"*64],annotation());self.assertEqual(results["b"*64],annotation())
            first_key=runner.task_id("quality_annotation",a)
            self.assertEqual(first_key,runner.task_id("quality_annotation",b))
            restarted=QualityRunner(Path(td),**kwargs);restarted.bind_admission({"a"*64:admitted})
            restarted.run_additional_stage("quality_annotation",[a]);self.assertEqual(len(calls),2)
            changed=copy.deepcopy(admitted);changed["admission"]["features"]["intent_reason"]="同一会话的新观察依据"
            restarted.bind_admission({"a"*64:changed})
            self.assertNotEqual(first_key,restarted.task_id("quality_annotation",a))
            restarted.run_additional_stage("quality_annotation",[a]);self.assertEqual(len(calls),3)
            attempts=[json.loads(x) for x in (Path(td)/"quality_annotation_attempts.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([x["outcome"] for x in attempts],["failure","success","success"])

    def test_all_grades_and_exact_legacy_vocabulary(self):
        for score in range(1, 6):
            self.assertEqual(validate_quality_annotation(annotation(score)), annotation(score))
        self.assertEqual(len(ISSUE_TYPES), 10)
        for set_name in ("P0", "P1", "P2"):
            fields = labeled_fields(set_name)
            self.assertEqual(len(fields), 29)
            self.assertEqual(len(set(fields)), 29)
            self.assertTrue(set(QUALITY_FIELDS).issubset(fields))
            self.assertNotIn("quality_task_difficulty", fields)

    def test_invalid_types_missing_fields_and_cross_field_contradictions(self):
        valid = annotation()
        bad_values = [
            {**valid, "quality_overall_score": True}, {**valid, "quality_overall_score": 5.0},
            {**valid, "quality_overall_score": 0}, {**valid, "quality_validity": []},
            {**valid, "quality_intent_clarity": "clarification"},
            {**valid, "quality_issue_types": []}, {**valid, "quality_issue_types": ["none", "multi_intent"]},
            {**valid, "quality_issue_types": ["ambiguous_intent", "ambiguous_intent"]},
            {**valid, "quality_issue_types": ["attachment_dependency"]},
            {**valid, "quality_validity": "borderline"}, {**annotation(3), "quality_issue_types": ["none"]},
            {**annotation(1), "quality_intent_clarity": "clear"},
            {**annotation(2), "quality_intent_clarity": "no_intent"},
            {**annotation(4), "quality_issue_types": ["no_evaluable_goal"]},
            {**valid, "intent_label": "qa"},
            {key: value for key, value in valid.items() if key != "quality_validity"},
        ]
        for bad in bad_values:
            with self.subTest(value=bad), self.assertRaises(QualityResponseError):
                validate_quality_annotation(bad)

    def test_full_input_binds_policy_and_clarification_without_downgrading(self):
        candidate = {"conversation": parse_conversation("帮我设闹钟"), "taxonomy": {"l1": "设备", "l2": "闹钟"}}
        admission = build_admission_result(feature(["帮我设闹钟"], points=("parameter",)), CATEGORY)
        payload = quality_annotation_input(candidate, admission)
        self.assertEqual(payload["conversation_turns"], ["帮我设闹钟"])
        self.assertEqual(payload["admission"]["screening"]["route"], "clarification")
        self.assertEqual(payload["admission"]["policy"], admission["admission"]["policy"])
        self.assertEqual(validate_quality_annotation(annotation())["quality_overall_score"], 5)

    def test_public_features_and_collection_scores(self):
        stats = {"total_count": 10, "less_count": 8, "min_frequency": 1, "max_frequency": 99}
        features = compute_fixed_features({"cluster_probability": 0.9, "frequency": 99}, parse_conversation("打开空调"), stats)
        self.assertNotIn("relevance", features)
        self.assertEqual(features["probability_band"], "high")
        self.assertEqual(features["frequency_band"], "hot")
        self.assertEqual(features["normalized_frequency"], 1)
        self.assertEqual(features["length_band"], "short")
        self.assertEqual(features["formality"], "formal")
        scores = sampling_scores({"signals": {"cluster_probability": 0.9}, "features": features})
        self.assertAlmostEqual(scores["p0"], 0.94)
        self.assertAlmostEqual(scores["p2"], 0.05)
        for probability, expected in ((0.499999, "boundary"), (0.5, "mid"), (0.899999, "mid"), (0.9, "high")):
            got = compute_fixed_features({"cluster_probability": probability, "frequency": 1}, parse_conversation("打开空调"), {"total_count": 1, "less_count": 0, "min_frequency": 1, "max_frequency": 1})
            self.assertEqual(got["probability_band"], expected)
            self.assertEqual(got["normalized_frequency"], 1)
        for less, band in ((1, "cold"), (2, "warm"), (7, "warm"), (8, "hot")):
            got = compute_fixed_features({"cluster_probability": 0.5, "frequency": 9}, parse_conversation("开灯"), {**stats, "less_count": less})
            self.assertEqual(got["frequency_band"], band)
        for length, band in ((9, "short"), (10, "mid"), (30, "mid"), (31, "long")):
            got = compute_fixed_features({"cluster_probability": 0.5, "frequency": 1}, parse_conversation("字"*length), {"total_count": 1, "less_count": 0, "min_frequency": 1, "max_frequency": 1})
            self.assertEqual(got["length_band"], band)
        self.assertEqual([_formality(text) for text in ("打开空调", "开灯呢嘛", "呢嘛吧啊哦哈啦呗呀哇")], ["formal", "mixed", "colloquial"])


class StageBoundaryTests(unittest.TestCase):
    def _profile(self, root):
        texts = ["打开客厅灯", "帮我设闹钟", "把音量降低一点", "给妈妈拨打电话", "播放音乐 | 换成古典音乐",
                 "先查询附近餐厅，再比较价格并选择最近一家", "先查询天气，然后把结果发送给妈妈", "写一封请假邮件"]
        source = root / "cluster.jsonl"
        source.write_text("".join(json.dumps({"session":f"s{i}","text":text,"cluster":1,"cluster_prob":0.4 if i%2 else 0.95,"frequency":i+1},ensure_ascii=False)+"\n" for i,text in enumerate(texts)),encoding="utf-8")
        taxonomy = root / "taxonomy.json"
        taxonomy.write_text(json.dumps({"categories":[{"name":"设备","total_size":len(texts),"assigned_clusters":[1],"sub_intents":[{"name":"控制","size":len(texts),"cluster_ids":[1]}]}]},ensure_ascii=False),encoding="utf-8")
        risk = root / "risk.xlsx"
        book=Workbook();book.active.title="Sheet1";book.active.append(["关键词","风险级别"]);book.active.append(["关机","高"]);book.save(risk)
        profile=json.loads((PACKAGE/"artifacts/config/pipeline.default.json").read_text(encoding="utf-8"))
        profile["inputs"]={"source":str(source),"taxonomy":str(taxonomy),"risk_keywords":str(risk)}
        profile["outputs"]["output_root"]=str(root/"outputs")
        for key in profile["prompts"]:
            profile["prompts"][key]=str((PACKAGE/"artifacts/config"/profile["prompts"][key]).resolve())
        profile["quality"]={"workers":3,"max_retries":1,"sleep_seconds":0}
        profile["index"].update(min_free_space_gb=0,reserve_free_space_gb=0,sqlite_cache_mb=1)
        profile["sampling"]["p1"]["target_n"]=8
        profile["sampling"]["p2"].update(target_n=2,batch_size=4,soft_min_by_primary={key:0 for key in profile["sampling"]["p2"]["soft_min_by_primary"]})
        path=root/"pipeline.json";path.write_text(json.dumps(profile),encoding="utf-8")
        return path

    def test_annotation_failure_freeze_independent_publication_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);profile=self._profile(root);calls=[];client=fake_client(calls)
            freeze_observed=[]
            def fail_one(messages, **kwargs):
                prompt=messages[0]["content"]
                if prompt.startswith("# 下游质量补充标注"):
                    runs=list((root/"outputs/runs").iterdir())
                    identity_payload=json.loads(prompt[prompt.rfind("\n{")+1:])
                    matching=[]
                    for name in ("P0","P1","P2"):
                        snapshot=runs[0]/name/"stage1.frozen.jsonl"
                        if snapshot.exists():
                            matching.extend(row for row in (json.loads(x) for x in snapshot.read_text(encoding="utf-8").splitlines())
                                if row["candidate"]["conversation"]["full_text"].split(" | ")==identity_payload["conversation_turns"])
                    freeze_observed.append(bool(matching) and all("downstream_quality" not in row and "delivery_labels" not in row for row in matching))
                    if identity_payload["conversation_turns"][-1]=="帮我设闹钟":
                        raise RuntimeError("synthetic quality annotation failure")
                return client(messages,**kwargs)
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                code=pipeline_main(["--config",str(profile),"--publish-root",str(root/"published")],llm_config_override=CONFIG,llm_call_override=fail_one)
            self.assertEqual(code,2)
            self.assertTrue(freeze_observed and all(freeze_observed))
            run=next((root/"outputs/runs").iterdir())
            manifest=json.loads((run/"manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["branches"]["P0"]["status"],"published")
            self.assertEqual(manifest["branches"]["P1"]["status"],"failed")
            self.assertFalse(list((run/"P1").glob("*_labeled.jsonl")))
            frozen_before={name:(run/name/"stage1.frozen.jsonl").read_bytes() for name in ("P0","P1","P2")}
            old_counts=Counter(prompt.splitlines()[0] for prompt in calls)
            from screening.pipeline import orchestrator
            with patch.object(orchestrator,"_sample_branch",side_effect=AssertionError("frozen Stage1 must not resample")), patch.object(orchestrator,"_run_adaptive_p2",side_effect=AssertionError("frozen P2 must not rebuild")), contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                code=pipeline_main(["--config",str(profile),"--run-id",manifest["run_id"],"--publish-root",str(root/"published")],llm_config_override=CONFIG,llm_call_override=client)
            self.assertEqual(code,0)
            self.assertEqual(frozen_before,{name:(run/name/"stage1.frozen.jsonl").read_bytes() for name in ("P0","P1","P2")})
            counts=Counter(prompt.splitlines()[0] for prompt in calls)
            for header in ("# 准入评估：评测目标、澄清点与必要依赖识别", "# 准入评估：独立类别核验"):
                self.assertEqual(counts[header],old_counts[header])
            self.assertEqual(len(list((root/"published").rglob("*_v4_labeled.jsonl"))),3)
            collect_and_validate_artifacts(run)
            database=next((root/"outputs/_canonical_cache").rglob("canonical_candidate_index.sqlite"))
            with contextlib.closing(sqlite3.connect(database)) as conn:
                columns={row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
            self.assertIn("formality",columns);self.assertNotIn("relevance_p0",columns);self.assertNotIn("relevance_p2",columns)
            frozen=run/"P1/stage1.frozen.jsonl";before=frozen.read_bytes()
            rows=[json.loads(line) for line in before.decode("utf-8").splitlines()]
            rows[0]["selection"]["admission_feature_task_id"]="0"*64
            frozen.write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),encoding="utf-8")
            with self.assertRaises((PublicationError,QualityResponseError)):
                collect_and_validate_artifacts(run)
            frozen.write_bytes(before)
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                conflict=pipeline_main(["--config",str(profile),"--run-id",manifest["run_id"],"--quality-annotation-prompt",str(PACKAGE/"artifacts/prompts/quality_admission.md")],llm_config_override=CONFIG,llm_call_override=client)
            self.assertEqual(conflict,1)


if __name__ == "__main__":
    unittest.main()
