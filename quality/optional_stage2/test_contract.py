"""Tests shipped with the removable optional Stage2 module."""
from pathlib import Path
import json
import unittest
from screening.quality.optional_stage2 import validate_stage2_result
from screening.quality.stage1 import QualityResponseError


class OptionalStage2ContractTests(unittest.TestCase):
    def test_contract_and_illustrative_cases(self):
        cases=json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))
        self.assertEqual(cases["contract_version"],1)
        for case in cases["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(validate_stage2_result(case["expected"]),case["expected"])

    def test_two_fields_and_issue_invariants(self):
        valid={"task_difficulty":"simple","execution_issues":[]}
        self.assertEqual(validate_stage2_result(valid),valid)
        for bad in ({**valid,"intent_label":"task"},{**valid,"task_difficulty":"not_applicable"},
                    {**valid,"execution_issues":["no_evaluable_goal"]},
                    {"task_difficulty":"not_applicable","execution_issues":["no_evaluable_goal","other_unfit"]}):
            with self.assertRaises(QualityResponseError): validate_stage2_result(bad)
        validate_stage2_result({"task_difficulty":"not_applicable","execution_issues":["no_evaluable_goal"]})
