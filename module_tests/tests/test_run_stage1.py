"""Admission semantics, isolated requests, replay and production round trips."""
from __future__ import annotations
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from openpyxl import Workbook, load_workbook

from screening.quality.stage1 import (DEFAULT_POLICY, QualityResponseError, build_stage1_result,
    validate_features, validate_stage1_result, screen, stage1_passes)
from screening.quality.runner import QUALITY_CONTRACT_VERSION, QualityRunner, QualityRunConfig, QualityStageIncomplete
from screening.module_tests import run_stage1 as standalone
from screening.delivery.eligibility import delivery_labels
from screening.delivery.publish import collect_and_validate_artifacts, validate_manifest_artifacts, PublicationError
from screening.pipeline.cli import main as pipeline_main
from screening.shared.llm import LlmConfig
from screening.shared.risk_keywords import load_risk_keywords

PACKAGE = Path(__file__).resolve().parents[2]
CONFIG = LlmConfig(api_url="https://offline.example.invalid/v1/chat/completions", api_key="offline-secret",
    model="offline-model", max_tokens=4096, timeout=3, stream=False, enable_thinking=False, skip_special_tokens=False)
CATEGORY = {"status": "reviewed", "fit": "exact"}


def feature(turns, *, anchor="task", goal="处理当前请求", points=(), deps=(), broken=False):
    return {"text_state": "broken" if broken else "recoverable", "anchor": anchor,
        "goal": goal, "objects": [], "constraints": [],
        "open_points": [{"kind": kind, "description": "需要确定的信息"} for kind in points],
        "dependencies": [{"source": source, "description": "必要依赖", "locator": turns[-1] if source in DEFAULT_POLICY["runtime_capabilities"] else None} for source in deps]}


def record(target="打开客厅灯", context="", category=True):
    turns = (context.split(" | ") if context else []) + [target]
    return {"sample_id": "reference", "context_text": context, "target_query": target,
        "sample_type": "multi_turn" if context else "single_turn", "conversation_turns": turns,
        "candidate_category": {"L1": "设备", "L2": "控制"} if category else {"L1": None, "L2": None}}


def fake_features(turns):
    target = turns[-1]
    if target in {"闹钟", "冰糖冬枣", "视频剪辑", "临沂"}:
        return feature(turns, anchor="topic", goal=None, points=("intent",))
    if target in {"嗯", "这样", "他说也不错", "下个路口左转"}:
        return feature(turns, anchor="none", goal=None)
    if target == "帮我设闹钟":
        return feature(turns, points=("parameter",))
    if target == "总结这份文件":
        return feature(turns, deps=("missing_material",))
    if target == "选择第二个":
        return feature(turns, deps=("missing_context",))
    if target == "总结当前视频":
        return feature(turns, deps=("current_screen_media",))
    return feature(turns)


def fake_client(calls):
    def call(messages, **kwargs):
        prompt = messages[0]["content"]
        calls.append(prompt)
        if prompt.startswith("# Stage1：基于文本"):
            payload = json.loads(prompt[prompt.rfind("\n{")+1:])
            assert set(payload) == {"sample_type", "conversation_turns"}
            value = fake_features(payload["conversation_turns"])
        elif prompt.startswith("# Stage1：独立"):
            payload = json.loads(prompt[prompt.rfind("\n{")+1:])
            assert set(payload) == {"sample_type", "conversation_turns", "candidate_category"}
            value = {"category_fit": "exact"}
        elif prompt.startswith("# P2"):
            payload = json.loads(prompt[prompt.rfind("\n{")+1:])
            assert "stage1_features" in payload and "screening" not in payload
            value = {"is_challenge": True, "primary_challenge": "multi_constraint", "challenge_types": ["multi_constraint"]}
        elif prompt.startswith("# 第二阶段"):
            payload = json.loads(prompt[prompt.rfind("\n{")+1:])
            assert set(payload) == {"sample_type", "conversation_turns"}
            # Deliberate cross-stage disagreement must not veto direct eligibility.
            value = {"task_difficulty": "simple", "intent_label": "qa", "execution_issues": ["attachment_dependency"]}
        else:
            return "OK", 1.0, 2.0
        return json.dumps(value, ensure_ascii=False), 1.0, 2.0
    return call


class Stage1ContractTests(unittest.TestCase):
    def test_objects_and_constraints_retained_without_excerpt_fields(self):
        turns=["帮我设置工作日早上的闹钟"]
        observation=feature(turns,points=("parameter",),deps=("user_to_specify",))
        observation["objects"]=["闹钟"]
        observation["constraints"]=["工作日","早上"]
        result=build_stage1_result(observation,CATEGORY,turns=turns)
        self.assertEqual(result["stage1"]["features"],observation)
        self.assertEqual(result["stage1"]["screening"]["route"],"clarification")
        self.assertNotIn('"evidence"',json.dumps(result))
        for field in ("open_points","dependencies"):
            old=copy.deepcopy(observation);old[field][0]["evidence"]=[]
            with self.assertRaises(QualityResponseError): validate_features(old,turns)

    def test_versioned_illustrative_fixtures(self):
        fixture=json.loads((PACKAGE/"artifacts/evals/stage1_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["quality_contract_version"],QUALITY_CONTRACT_VERSION)
        for case in fixture["cases"]:
            with self.subTest(case=case["id"]):
                result=build_stage1_result(case["features"],case["category"],case["policy"],turns=case["input"]["conversation_turns"])
                self.assertEqual(result["stage1"]["screening"],case["expected_screening"])

    def test_ordered_rules_and_independent_category(self):
        for target, expected in (("打开客厅灯", "direct"), ("闹钟", "clarification"),
                ("冰糖冬枣", "clarification"), ("临沂", "clarification"), ("帮我设闹钟", "clarification"),
                ("嗯", "excluded"), ("这样", "excluded"), ("他说也不错", "excluded"), ("下个路口左转", "excluded"),
                ("总结这份文件", "excluded"), ("选择第二个", "excluded"), ("总结当前视频", "direct")):
            with self.subTest(target=target):
                result = build_stage1_result(fake_features([target]), CATEGORY, turns=[target])
                self.assertEqual(result["stage1"]["screening"]["route"], expected)
                self.assertEqual(stage1_passes(result), expected != "excluded")
        f = feature(["总结文件并设闹钟"], points=("parameter",), deps=("missing_material", "missing_context"))
        result = build_stage1_result(f, {"status": "reviewed", "fit": "partial"})
        self.assertEqual(result["stage1"]["screening"]["reasons"], ["missing_material", "missing_context", "category_partial"])

    def test_replay_policy_and_invalid_observation_or_decision(self):
        f = fake_features(["闹钟"])
        default = build_stage1_result(f, CATEGORY)
        policy = copy.deepcopy(DEFAULT_POLICY); policy["allowed_routes"] = ["direct"]
        self.assertFalse(stage1_passes(build_stage1_result(f, CATEGORY, policy)))
        bad = copy.deepcopy(default); bad["stage1"]["screening"]["admitted"] = False
        with self.assertRaises(QualityResponseError): validate_stage1_result(bad)
        bad_feature = copy.deepcopy(f); bad_feature["evidence"] = []
        with self.assertRaises(QualityResponseError): validate_features(bad_feature, ["闹钟"])
        runtime = fake_features(["总结当前视频"])
        bad_feature = copy.deepcopy(runtime); bad_feature["dependencies"][0]["locator"] = "虚构文件路径"
        with self.assertRaises(QualityResponseError): validate_features(bad_feature, ["总结当前视频"])
        runtime = fake_features(["总结当前视频"])
        policy["runtime_capabilities"] = []
        self.assertIn("runtime_unavailable", screen(runtime, CATEGORY, policy)["reasons"])
        f["text_state"] = "broken"
        self.assertEqual(screen(f, CATEGORY, DEFAULT_POLICY)["route"], "excluded")

    def test_missing_category_does_not_change_text_route(self):
        f = fake_features(["冰糖冬枣"])
        result = build_stage1_result(f, {"status": "not_provided", "fit": None})
        self.assertEqual(result["stage1"]["screening"]["route"], "clarification")
        self.assertEqual(result["stage1"]["screening"]["reasons"], ["category_not_provided"])

    def test_executable_retains_other_filters_and_stage2_intent(self):
        with tempfile.TemporaryDirectory() as td:
            risk_path = Path(td)/"risk.xlsx"
            workbook = Workbook(); workbook.active.title = "Sheet1"; workbook.active.append(["关键词", "风险级别"]); workbook.active.append(["关机", "高"]); workbook.save(risk_path)
            risk = load_risk_keywords(risk_path)
            stage2 = {"intent_label": "qa", "execution_issues": ["attachment_dependency"]}
            for turns, expected in ((["打开客厅灯"],1), (["暂停"],0), (["闹钟"],0), (["帮我设闹钟"],0), (["请关机"],0), (["开灯", "调暗一点"],0)):
                candidate = {"conversation": {"target_query": turns[-1], "sample_type": "multi_turn" if len(turns)>1 else "single_turn"}}
                stage1 = build_stage1_result(fake_features(turns), CATEGORY)
                labels = delivery_labels(candidate, stage2, risk, stage1)
                self.assertEqual(labels["executable"], expected)
                self.assertEqual(labels["intent_label"], "qa")
                self.assertEqual(bool(labels["executable_reasons"]), not expected)


class Stage1ModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)

    def write(self, rows, name="input.jsonl"):
        path = self.root/name; path.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in rows),encoding="utf-8"); return path

    def run_samples(self, rows, **kwargs):
        path = self.write(rows); calls=[]
        with contextlib.redirect_stdout(io.StringIO()):
            code = standalone.run_stage1_test(path,self.root/"out",llm_config=CONFIG,llm_call=fake_client(calls),**kwargs)
        results=[json.loads(x) for x in (self.root/"out/results.jsonl").read_text(encoding="utf-8").splitlines()]
        return code,calls,results

    def test_reference_duplicates_category_skip_and_replay_without_credentials(self):
        code,calls,rows=self.run_samples([record("冰糖冬枣",category=False),record("冰糖冬枣",category=False),record()])
        self.assertEqual(code,0);self.assertEqual(len(calls),4)
        self.assertEqual([r["route"] for r in rows],["clarification","clarification","direct"])
        self.assertEqual(rows[0]["stage_status"]["category"],"not_provided")
        self.assertEqual([r["source_line"] for r in rows],[1,2,3])
        policy=copy.deepcopy(DEFAULT_POLICY);policy["allowed_routes"]=["direct"]
        with contextlib.redirect_stdout(io.StringIO()):
            code=standalone.run_stage1_test(self.root/"out/results.jsonl",self.root/"replay",replay=True,policy=policy)
        self.assertEqual(code,0)
        again=[json.loads(x) for x in (self.root/"replay/results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(again[0]["features"],rows[0]["features"])
        self.assertIn("route_disabled",again[0]["stage1_result"]["stage1"]["screening"]["reasons"])
        self.assertEqual(again[0]["observation_provenance"],rows[0]["observation_provenance"])
        workbook=load_workbook(self.root/"out/results.xlsx");self.assertEqual(workbook.active.max_row,4)
        tampered=copy.deepcopy(rows[2]);tampered["source_record"]["candidate_category"]["L2"]="其他"
        tampered["stage1_input"]["candidate_category"]["L2"]="其他"
        tampered_input=self.write([tampered],"tampered.jsonl")
        with contextlib.redirect_stdout(io.StringIO()):
            code=standalone.run_stage1_test(tampered_input,self.root/"bad-replay",replay=True)
        self.assertEqual(code,2)


    def test_invalid_inputs_and_output_directory(self):
        bad=record();bad["conversation_turns"]=["不同输入"]
        code,calls,rows=self.run_samples([bad,record("打开客厅灯", "旧话题")])
        self.assertEqual(code,2);self.assertEqual(rows[0]["status"],"input_error");self.assertIsNone(rows[0]["stage1_pass"])
        self.assertEqual(len(calls),2)
        with self.assertRaises(ValueError): standalone.run_stage1_test(self.root/"input.jsonl",self.root/"out",llm_config=CONFIG)
        old=record(); old.update(text="打开客厅灯",turn_count=1,L1="设备",L2="控制",intent_label="old-label")
        payload=standalone.build_stage1_input(old);self.assertNotIn("intent_label",payload)

    def test_category_failure_preserves_successful_features(self):
        calls=[];good=fake_client(calls)
        def client(messages,**kwargs):
            if messages[0]["content"].startswith("# Stage1：独立"): return "not json",0,0
            return good(messages,**kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            code=standalone.run_stage1_test(self.write([record()]),self.root/"out",llm_config=CONFIG,llm_call=client)
        row=json.loads((self.root/"out/results.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(code,2);self.assertEqual(row["status"],"response_error");self.assertIsNotNone(row["features"]);self.assertIsNone(row["stage1_pass"])

    def test_local_reference_shape_when_available(self):
        path=PACKAGE.parent/"当前透传到外部的样本仅供参考/query_samples_all.jsonl"
        if not path.exists(): self.skipTest("本地参考文件不随代码入库")
        rows=[json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(rows),202)
        payloads=[standalone.build_stage1_input(r) for r in rows]
        self.assertEqual(sum(p["sample_type"]=="single_turn" for p in payloads),149)
        self.assertTrue(all(p["candidate_category"]=={"L1":None,"L2":None} for p in payloads))


class RunnerIsolationTests(unittest.TestCase):
    def test_component_cache_category_isolation_retry_and_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            calls=[]
            client=fake_client(calls)
            attempts={"category":0}
            def flaky(messages,**kwargs):
                if messages[0]["content"].startswith("# Stage1：独立"):
                    attempts["category"]+=1
                    if attempts["category"]==1:
                        raise RuntimeError("do not log endpoint or credential here")
                return client(messages,**kwargs)
            kwargs=dict(stage1_prompt=standalone.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"),
                category_prompt=standalone.DEFAULT_CATEGORY_PROMPT_PATH.read_text(encoding="utf-8"),
                stage2_prompt=(PACKAGE/"artifacts/prompts/quality_stage2.md").read_text(encoding="utf-8"),
                challenge_prompt=(PACKAGE/"artifacts/prompts/quality_challenge.md").read_text(encoding="utf-8"),
                model=CONFIG.model,llm_options=CONFIG.options(),config=QualityRunConfig(workers=2,sleep_seconds=0,max_retries=1))
            from screening.candidate.contract import parse_conversation
            conversation=parse_conversation("打开客厅灯")
            a={"source_identity_sha256":"a"*64,"conversation_sha256":"c"*64,
                "conversation":conversation,"source":{"source_line":1},"taxonomy":{"l1":"设备","l2":"控制","l2_key":"设备/控制"}}
            b=copy.deepcopy(a);b["source_identity_sha256"]="b"*64;b["source"]["source_line"]=2;b["taxonomy"]["l2"]="其他"
            runner=QualityRunner(root,llm_call=flaky,**kwargs)
            with self.assertRaises(QualityStageIncomplete): runner.run_stage1([a])
            self.assertNotIn("do not log endpoint",(root/"stage1_category_attempts.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(len([p for p in calls if p.startswith("# Stage1：基于文本")]),1)
            # A fresh runner resumes the feature cache after category failure.
            restarted=QualityRunner(root,llm_call=client,**kwargs)
            first=restarted.run_stage1([a,b])
            self.assertEqual(set(first),{"a"*64,"b"*64})
            self.assertEqual(len([p for p in calls if p.startswith("# Stage1：基于文本")]),1)
            self.assertEqual(len([p for p in calls if p.startswith("# Stage1：独立")]),2)
            before=len(calls);restarted.run_stage1([a,b]);self.assertEqual(len(calls),before)
            self.assertNotEqual(restarted.task_id("stage1_category",a),restarted.task_id("stage1_category",b))
            self.assertEqual(restarted.task_id("stage1_features",a),restarted.task_id("stage1_features",b))
            policy=copy.deepcopy(DEFAULT_POLICY);policy["allowed_routes"]=["direct"]
            with self.assertRaises(ValueError): QualityRunner(root,llm_call=client,screening_policy=policy,**kwargs)


class ProductionRoundTripTests(unittest.TestCase):
    def test_pipeline_resume_export_publish_and_policy_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/"cluster.jsonl";taxonomy=root/"taxonomy.json";risk=root/"risk.xlsx"
            texts=["打开客厅灯","帮我设闹钟","冰糖冬枣","总结当前视频","请关机","暂停",
                "把音量降低一点","给妈妈拨打电话","播放音乐 | 换成古典音乐", "先查询附近餐厅，再比较价格并选择最近一家",
                "明早六点叫我 | 改成七点", "先查询天气，然后把结果发送给妈妈", "添加日历 | 添加桌面日历 | 嗯，是的",
                "总结这份文件", "选择第二个"]
            source.write_text("".join(json.dumps({"session":f"s{i}","text":text,"cluster":1,"cluster_prob":0.4 if i%2 else 0.95,"frequency":i+1},ensure_ascii=False)+"\n" for i,text in enumerate(texts)),encoding="utf-8")
            taxonomy.write_text(json.dumps({"categories":[{"name":"设备","total_size":len(texts),"assigned_clusters":[1],"sub_intents":[{"name":"控制","size":len(texts),"cluster_ids":[1]}]}]},ensure_ascii=False),encoding="utf-8")
            workbook=Workbook();workbook.active.title="Sheet1";workbook.active.append(["关键词","风险级别"]);workbook.active.append(["关机","高"]);workbook.save(risk)
            profile=json.loads((PACKAGE/"artifacts/config/pipeline.default.json").read_text(encoding="utf-8"))
            profile["inputs"]={"source":str(source),"taxonomy":str(taxonomy),"risk_keywords":str(risk)}
            profile["outputs"]["output_root"]=str(root/"outputs")
            for k in profile["prompts"]: profile["prompts"][k]=str((PACKAGE/"artifacts/config"/profile["prompts"][k]).resolve())
            profile["quality"]={"workers":3,"max_retries":2,"sleep_seconds":0}
            profile["index"].update(min_free_space_gb=0,reserve_free_space_gb=0,sqlite_cache_mb=1)
            profile["sampling"]["p1"]["target_n"]=14;profile["sampling"]["p2"].update(target_n=3,batch_size=6,soft_min_by_primary={k:0 for k in profile["sampling"]["p2"]["soft_min_by_primary"]})
            config_path=root/"pipeline.json";config_path.write_text(json.dumps(profile,ensure_ascii=False),encoding="utf-8")
            calls=[];client=fake_client(calls)
            errors = io.StringIO()
            output = io.StringIO()
            with contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
                code=pipeline_main(["--config",str(config_path),"--publish-root",str(root/"live-delivery")],llm_config_override=CONFIG,llm_call_override=client)
            self.assertEqual(code,0, output.getvalue() + errors.getvalue()[-1200:])
            run=next((root/"outputs/runs").iterdir());manifest=json.loads((run/"manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"],"published")
            self.assertEqual(manifest["contract"]["labeled_mapping_version"],7)
            self.assertNotIn("legacy_mapping_version",manifest["contract"])
            self.assertEqual(len(list((root/"live-delivery").rglob("*_v6_labeled.jsonl"))),3)
            validate_manifest_artifacts(run,manifest)
            originals={r["session"]:r for r in (json.loads(line) for line in source.read_text(encoding="utf-8").splitlines())}
            all_records=[]
            for name in ("P0","P1","P2"):
                rows=[json.loads(x) for x in (run/name/"screened.canonical.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertTrue(rows);all_records+=rows
                exported=next((run/name).glob("*_v6_labeled.jsonl"))
                delivered=[json.loads(x) for x in exported.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(all(len(r)==15 and "stage1" in r and r["intent_label"]=="qa" for r in delivered))
                for item,internal in zip(delivered,rows):
                    original=originals[item["session_id"]]
                    self.assertEqual(item["frequency"],original["frequency"])
                    self.assertEqual(item["cluster_prob"],original["cluster_prob"])
                    self.assertEqual(item["cluster_id"],original["cluster"])
                    self.assertEqual(item["stage1"],internal["quality"]["stage1"])
            self.assertTrue(any(r["quality"]["stage1"]["screening"]["route"]=="clarification" for r in all_records))
            self.assertTrue(any(r["delivery_labels"]["executable"]==1 and r["quality"]["execution_issues"] for r in all_records))
            count=len(calls)
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                code=pipeline_main(["--config",str(config_path),"--publish-root",str(root/"delivery")],llm_config_override=CONFIG,llm_call_override=client)
            self.assertEqual(code,0);self.assertEqual(len(calls),count+1) # connectivity preflight only
            self.assertEqual(len(list((root/"delivery").rglob("*_v6_labeled.jsonl"))),3)
            feature_rows=run/"quality/stage1_features_results.jsonl";original=feature_rows.read_text(encoding="utf-8")
            modified=[json.loads(x) for x in original.splitlines()];modified[0]["result"]["goal"]="tampered";feature_rows.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in modified),encoding="utf-8")
            with self.assertRaises((PublicationError,QualityResponseError)): collect_and_validate_artifacts(run)
            feature_rows.write_text(original,encoding="utf-8")
            old=json.loads(config_path.read_text(encoding="utf-8"));old["config_version"]=7;config_path.write_text(json.dumps(old),encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()): self.assertEqual(pipeline_main(["--config",str(config_path)],llm_config_override=CONFIG,llm_call_override=client),1)


if __name__ == "__main__": unittest.main()
