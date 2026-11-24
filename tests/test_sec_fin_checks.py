"""sec-fin@0.2.0 check: its rules, and every shipped reference scores 1.0 against itself and 0.0 when perturbed."""
from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PKG = Path(__file__).resolve().parents[1] / "catalog" / "packages" / "sec-fin" / "0.2.0"
_spec = importlib.util.spec_from_file_location("sec_fin_020_answer", PKG / "checks" / "answer.py")
answer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(answer)
_spec = importlib.util.spec_from_file_location("llm_sql", PKG.parents[3] / "adapters" / "llm_sql.py")
llm_sql = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llm_sql)


def score(ref, got):
    r = answer.check({}, ref, {"answer": got})
    return r.value if r.valid else None


class Rules(unittest.TestCase):
    def test_null_reference(self):
        ref = {"answer": None, "answer_format": "number"}
        self.assertEqual(score(ref, None), 1.0)
        self.assertEqual(score(ref, 0.0), 0.0)
        self.assertEqual(score({"answer": None, "answer_format": "names"}, ["ACME"]), 0.0)

    def test_null_answer_to_a_present_fact_is_wrong_not_invalid(self):
        self.assertEqual(score({"answer": 5.0, "answer_format": "number"}, None), 0.0)
        self.assertEqual(score({"answer": ["A"], "answer_format": "names"}, None), 0.0)

    def test_missing_answer_field_is_invalid(self):
        self.assertFalse(answer.check({}, {"answer": None, "answer_format": "number"}, {}).valid)

    def test_default_relative_tolerance(self):
        ref = {"answer": 1e9, "answer_format": "number"}
        self.assertEqual(score(ref, 1e9 + 999), 1.0)
        self.assertEqual(score(ref, 1e9 + 1001), 0.0)

    def test_absolute_tolerance(self):
        ref = {"answer": 0.2345, "answer_format": "number", "tolerance": {"abs": 0.0001}}
        for ok in (0.2345, 0.23454, 0.2344, 0.2346):
            self.assertEqual(score(ref, ok), 1.0, ok)
        for bad in (0.2343, 0.2347, 23.45):
            self.assertEqual(score(ref, bad), 0.0, bad)

    def test_relative_tolerance_field(self):
        self.assertEqual(score({"answer": 100.0, "answer_format": "number", "tolerance": {"rel": 0.01}}, 100.9), 1.0)
        self.assertEqual(score({"answer": 100.0, "answer_format": "number", "tolerance": {"rel": 0.01}}, 101.1), 0.0)

    def test_bad_types_are_invalid(self):
        for got in (True, "12", float("nan"), [1.0]):
            self.assertIsNone(score({"answer": 12.0, "answer_format": "number"}, got), got)
        self.assertIsNone(score({"answer": ["A"], "answer_format": "names"}, "A"))

    def test_names_are_a_case_and_space_insensitive_set(self):
        ref = {"answer": ["ACME CORP", "BETA INC"], "answer_format": "names"}
        self.assertEqual(score(ref, ["beta  inc", "Acme Corp"]), 1.0)
        self.assertEqual(score(ref, ["ACME CORP"]), 0.0)


def perturb(ref):
    a = ref["answer"]
    if a is None:
        return 1.0
    if ref["answer_format"] == "names":
        return a[1:] if len(a) > 1 else a + ["NOT A FILER"]
    return a * 1.01 + 1


class ShippedReferences(unittest.TestCase):
    def test_every_reference_scores_itself_and_fails_perturbed(self):
        tasks = tomllib.loads((PKG / "tasks.toml").read_text())["task"]
        self.assertEqual(len(tasks), 100)
        for t in tasks:
            ref = json.loads((PKG / t["reference_ref"]).read_text())
            with self.subTest(t["task_id"]):
                self.assertEqual(score(ref, ref["answer"]), 1.0)
                self.assertEqual(score(ref, perturb(ref)), 0.0)

    def test_every_gold_query_scores_through_the_adapter(self):
        """The adapter's own result conversion, so unanswerable gold SQL must abstain the way a candidate does."""
        for t in tomllib.loads((PKG / "tasks.toml").read_text())["task"]:
            ref = json.loads((PKG / t["reference_ref"]).read_text())
            with self.subTest(t["task_id"]):
                got = llm_sql.execute(PKG / "data" / "fsds.sqlite", ref["sql"], ref["answer_format"])
                self.assertEqual(score(ref, got), 1.0)


if __name__ == "__main__":
    unittest.main()
