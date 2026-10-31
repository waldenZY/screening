"""Run the actual Bash wrapper against temporary datasets and the simulated pipeline."""
import contextlib
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from openpyxl import Workbook
from screening.module_tests.tests.test_run_admission import CONFIG, PACKAGE


def find_bash():
    return shutil.which("bash") or next((str(path) for path in (
        Path("D:/Application/Git/bin/bash.exe"),Path("C:/Program Files/Git/bin/bash.exe")) if path.is_file()),None)


def bash_path(path):
    value=Path(path).resolve().as_posix()
    return "/"+value[0].lower()+value[2:] if os.name=="nt" and len(value)>2 and value[1]==":" else value


@unittest.skipUnless(find_bash(),"Bash未安装；在Linux或Git Bash环境执行批跑回归")
class BatchRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.package=self.root/"screening";self.package.mkdir()
        for name in ("candidate","delivery","pipeline","quality","shared","module_tests"):
            shutil.copytree(PACKAGE/name,self.package/name,ignore=shutil.ignore_patterns("__pycache__","outputs"))
        shutil.copy2(PACKAGE/"__init__.py",self.package/"__init__.py")
        shutil.copytree(PACKAGE/"artifacts/prompts",self.package/"artifacts/prompts")
        (self.package/"artifacts/config").mkdir()
        profile=json.loads((PACKAGE/"artifacts/config/pipeline.default.json").read_text(encoding="utf-8"))
        profile["quality"]={"workers":2,"max_retries":1,"sleep_seconds":0}
        profile["index"].update(min_free_space_gb=0,reserve_free_space_gb=0,sqlite_cache_mb=1)
        profile["sampling"]["p1"]["target_n"]=2
        profile["sampling"]["p2"].update(target_n=1,batch_size=2,soft_min_by_primary={key:0 for key in profile["sampling"]["p2"]["soft_min_by_primary"]})
        self.config=self.package/"artifacts/config/pipeline.default.json"
        self.config.write_text(json.dumps(profile),encoding="utf-8")
        (self.package/"artifacts/config/llm_config.local.json").write_text(json.dumps({
            "api_url":CONFIG.api_url,"api_key":CONFIG.api_key,"model":CONFIG.model,**CONFIG.options()}),encoding="utf-8")
        # Run the real pipeline with its injected fake transport, never the live endpoint.
        (self.package/"run_pipeline.py").write_text(
            "from screening.pipeline.cli import main\n"
            "from screening.module_tests.tests.test_run_admission import CONFIG, fake_client\n"
            "raise SystemExit(main(llm_config_override=CONFIG, llm_call_override=fake_client([])))\n",encoding="utf-8")
        self.tags=["261001-1007-7days","261008-1014-7days"]
        self.datasets=[]
        for tag in self.tags:
            folder=self.package/"fixtures"/tag;folder.mkdir(parents=True);self.datasets.append(folder)
            (folder/"cluster.jsonl").write_text("".join(json.dumps({"session":f"s{i}","text":text,
                "cluster":1,"cluster_prob":0.4 if i else 0.95,"frequency":i+1},ensure_ascii=False)+"\n"
                for i,text in enumerate(("打开客厅灯","把音量降低一点"))),encoding="utf-8")
            (folder/"taxonomy.json").write_text(json.dumps({"categories":[{"name":"设备","total_size":2,
                "assigned_clusters":[1],"sub_intents":[{"name":"控制","size":2,"cluster_ids":[1]}]}]},ensure_ascii=False),encoding="utf-8")
        book=Workbook();book.active.title="Sheet1";book.active.append(["关键词","风险级别"]);book.active.append(["关机","高"])
        book.save(self.package/"fixtures/risk.xlsx");book.close()
        script=(PACKAGE/"run_screening_batch.sh").read_text(encoding="utf-8")
        script=script.replace('"/data/project_a"',f'"fixtures/{self.tags[0]}"').replace('"/data/project_b"',f'"fixtures/{self.tags[1]}"')
        script=script.replace('RISK_KEYWORDS="/data/common/risk_keywords.xlsx"','RISK_KEYWORDS="fixtures/risk.xlsx"')
        script=script.replace('OUTPUT_BASE="batch_outputs"','OUTPUT_BASE="batch outputs"')
        self.script=self.package/"run_screening_batch.sh"
        self.script.write_text(script,encoding="utf-8",newline="\n")
        self.env={**os.environ,"SCREENING_PYTHON":bash_path(sys.executable),"PYTHONIOENCODING":"utf-8"}

    def run_bash(self,action):
        return subprocess.run([find_bash(),str(self.script),action],cwd=self.root.parent,
            env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding="utf-8",errors="replace",timeout=60)

    def summary(self):
        # Bash records POSIX paths on Windows; use the known local runs directory instead.
        run=next((self.package/".screening_batch/runs").iterdir())
        with (run/"summary.tsv").open(encoding="utf-8",newline="") as handle:
            return run,list(csv.DictReader(handle,delimiter="\t"))

    def test_syntax_and_read_only_check_uses_v11_four_prompts(self):
        syntax=subprocess.run([find_bash(),"-n",str(PACKAGE/"run_screening_batch.sh")],capture_output=True,text=True)
        self.assertEqual(syntax.returncode,0,syntax.stderr)
        result=self.run_bash("check")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("Pipeline config v11",result.stdout)
        self.assertIn("No pipeline or LLM request",result.stdout)
        self.assertFalse((self.package/".screening_batch").exists())
        self.assertFalse((self.package/"batch outputs").exists())
        profile=json.loads(self.config.read_text(encoding="utf-8"));profile["config_version"]=10
        self.config.write_text(json.dumps(profile),encoding="utf-8")
        result=self.run_bash("check")
        self.assertNotEqual(result.returncode,0)
        self.assertIn("local pipeline validation failed",result.stderr)

    def test_two_datasets_full_new_pipeline_and_downstream_layout(self):
        result=self.run_bash("run")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        run,rows=self.summary()
        self.assertEqual([row["status"] for row in rows],["SUCCESS","SUCCESS"])
        self.assertEqual((run/"status").read_text().strip(),"SUCCESS")
        for tag in self.tags:
            files=list((self.package/"batch_publications"/tag/"evaluation-set-large").rglob("*_v4_labeled.jsonl"))
            self.assertEqual(len(files),3)
            for file in files:
                records=[json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(records)
                self.assertTrue(all(len(row)==29 and "admission" in row["stage1"] for row in records))
            self.assertTrue((self.package/"batch outputs"/tag/"runs").is_dir())
        status=self.run_bash("status")
        self.assertEqual(status.returncode,0);self.assertIn("success=2 failed=0",status.stdout)
        self.assertFalse((self.package/".screening_batch/current.pid").exists())

    def test_failed_dataset_does_not_stop_next_dataset(self):
        (self.datasets[0]/"cluster.jsonl").write_text("invalid JSON\n",encoding="utf-8")
        result=self.run_bash("run")
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        run,rows=self.summary()
        self.assertEqual([row["status"] for row in rows],["FAILED_PIPELINE","SUCCESS"])
        self.assertEqual((run/"status").read_text().strip(),"PARTIAL_FAILED")
        self.assertEqual(len(list((self.package/"batch_publications"/self.tags[1]/"evaluation-set-large").rglob("*_v4_labeled.jsonl"))),3)

    def test_background_start_without_executable_bit_and_status(self):
        self.script.chmod(0o644)
        result=self.run_bash("start")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        deadline=time.monotonic()+45
        run=next((self.package/".screening_batch/runs").iterdir())
        while (run/"status").read_text().strip()=="RUNNING" and time.monotonic()<deadline:
            time.sleep(0.1)
        self.assertEqual((run/"status").read_text().strip(),"SUCCESS",(run/"batch.log").read_text(encoding="utf-8",errors="replace"))
        status=self.run_bash("status")
        self.assertEqual(status.returncode,0);self.assertIn("success=2 failed=0",status.stdout)


if __name__=="__main__":unittest.main()
