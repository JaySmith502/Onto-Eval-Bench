"""The PRD's hard rules as executable checks against the model kernel.

Run: python -m unittest discover -s tests   (from the repo root, with src/ on PYTHONPATH)
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from observatory.model import (  # noqa: E402
    AdapterRef, AnalysisPlan, Attempt, BenchmarkPackage, Budget, BuildRecord, Cohort, ConfigurationIdentity,
    Correction, CorrectionKind, Cost, Disclosure, ExecutableCheck, Incomparable, LadderLevel, Mode, Outcome,
    PackageRef, Purpose, RunManifest, RunPlan, RunRecord, ScorerKind, ScoringDecision, Seal, Split, Status,
    Task, Track, Verdict, VoidReason,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUDGET = Budget(max_usd=5.0, build_timeout_s=600, task_timeout_s=60, memory_mb=2048, max_attempts=2)
REF = PackageRef("relfin", "1.0.0", "abc")
TASKS = tuple(
    Task(f"t{i}", f"g{i % 2}", frozenset({Track.SEMANTIC_TASK}), frozenset({"task:querying"}), Split.HELDOUT,
         Disclosure.PUBLIC, "easy", "low", f"in/t{i}.json", f"ref/t{i}.json", ExecutableCheck("checks/eq.py:check", "v1"))
    for i in range(4)
)
PKG = BenchmarkPackage(
    ref=REF, suite="frozen_core", provenance="p", usage_rights="cc-by", taxonomy_version="1", data=(), tasks=TASKS,
    budgets=((Mode.CONTROLLED, BUDGET), (Mode.NATIVE, BUDGET)), controlled_model=("m", "1"), permitted_tools=(),
    external_access=False, human_assistance="none",
    analysis=AnalysisPlan(min_meaningful_diff=0.05, bootstrap_resamples=1000, seed=7, min_groups=2, multiplicity="holm"),
)


def config(name: str, model: str = "m") -> ConfigurationIdentity:
    return ConfigurationIdentity(name, name, "1.0", model, "1", LadderLevel.AUTO_ONTOLOGY, (), frozenset({"schema"}),
                                 None, AdapterRef(("adapter",), "1"), True)


def run(cfg: ConfigurationIdentity, scores: dict[str, float], *, mode: Mode = Mode.CONTROLLED,
        package: PackageRef = REF, task_ids: tuple[str, ...] | None = None, purpose: Purpose = Purpose.SCORED,
        void: Correction | None = None) -> RunRecord:
    """scores: task_id -> value; a planned task absent from `scores` has no attempt at all."""
    plan = RunPlan(cfg, package, mode, Track.SEMANTIC_TASK, purpose, 1,
                   task_ids or tuple(t.task_id for t in TASKS), BUDGET)
    m = RunManifest(plan, NOW, "jay", (), ())
    attempts = tuple(Attempt(m.run_id, t, 0, 1, None, "out.json", "", 1.0, Cost(), NOW) for t in scores)
    decisions = tuple(
        ScoringDecision(f"{m.run_id}/{t}/0", m.manifest_id, ScorerKind.EXECUTABLE, "v1", Status.SCORED, Verdict(v),
                        "", (), NOW)
        for t, v in scores.items())
    return RunRecord(m, BuildRecord(m.run_id, None, "", 1.0, Cost()), attempts, (),
                     Seal(m.run_id, NOW, ()), decisions, void)


def cohort(*records: RunRecord) -> Cohort:
    p = records[0].manifest.plan
    return Cohort(p.key, PKG, {r.manifest.plan.config.config_id: (r,) for r in records})


ALL = {t.task_id: 1.0 for t in TASKS}


class HardRules(unittest.TestCase):
    def test_controlled_and_native_never_share_a_cohort(self):
        a, b = run(config("a"), ALL), run(config("b"), ALL, mode=Mode.NATIVE)
        with self.assertRaises(Incomparable):
            Cohort(a.manifest.plan.key, PKG, {"a": (a,), "b": (b,)})

    def test_controlled_runs_on_different_models_are_different_cohorts(self):
        a, b = run(config("a"), ALL), run(config("b", model="other"), ALL)
        self.assertNotEqual(a.manifest.plan.key, b.manifest.plan.key)

    def test_other_package_hash_is_incomparable(self):
        a = run(config("a"), ALL)
        b = run(config("b"), ALL, package=replace(REF, content_hash="zzz"))
        with self.assertRaises(Incomparable):
            Cohort(a.manifest.plan.key, PKG, {"a": (a,), "b": (b,)})

    def test_missing_result_is_zero_credit_not_dropped(self):
        r = run(config("a"), {"t0": 1.0, "t1": 1.0})
        self.assertEqual(len(r.outcomes), len(TASKS))
        self.assertEqual(r.outcomes[("t2", 0)].status, Status.MISSING)
        self.assertEqual(r.outcomes[("t2", 0)].credit, 0.0)

    def test_plan_omitting_tasks_is_rejected(self):
        short = run(config("a"), {"t0": 1.0}, task_ids=("t0",))
        with self.assertRaises(Incomparable):
            cohort(short)

    def test_smoke_and_voided_runs_cannot_be_ranked(self):
        with self.assertRaises(Incomparable):
            cohort(run(config("a"), ALL, purpose=Purpose.SMOKE))
        void = Correction(NOW, "ed", CorrectionKind.VOID_RUN, "x", "scorer crashed", VoidReason.HARNESS_BUG)
        with self.assertRaises(Incomparable):
            cohort(run(config("a"), ALL, void=void))

    def test_score_without_decision_cannot_exist(self):
        with self.assertRaises(ValueError):
            Outcome("r", "t0", 0, Status.SCORED, "m", Verdict(1.0), None)

    def test_adapter_side_cannot_claim_scored(self):
        with self.assertRaises(ValueError):
            Attempt("r", "t0", 0, 1, Status.SCORED, None, "", 1.0, Cost(), NOW)

    def test_dispute_is_unresolved_until_adjudicated(self):
        r = run(config("a"), ALL)
        exe = next(d for d in r.decisions if d.outcome_id.endswith("/t0/0"))
        review = ScoringDecision(exe.outcome_id, exe.manifest_id, ScorerKind.HUMAN_REVIEW, "ana", Status.SCORED,
                                 Verdict(0.0), "wrong join", (), NOW)
        disputed = replace(r, decisions=r.decisions + (review,))
        self.assertEqual(disputed.outcomes[("t0", 0)].status, Status.UNRESOLVED)
        self.assertEqual(disputed.outcomes[("t0", 0)].credit, 0.0)
        adj = ScoringDecision(exe.outcome_id, exe.manifest_id, ScorerKind.ADJUDICATION, "lee", Status.SCORED,
                              Verdict(0.5), "partially right", (exe.decision_id, review.decision_id), NOW)
        settled = replace(r, decisions=disputed.decisions + (adj,))
        self.assertEqual(settled.outcomes[("t0", 0)].credit, 0.5)
        self.assertEqual(settled.outcomes[("t0", 0)].decision_id, adj.decision_id)

    def test_build_failure_marks_every_task(self):
        r = run(config("a"), {})
        failed = replace(r, build=BuildRecord(r.run_id, Status.BUILD_FAILURE, "boom", 1.0, Cost()))
        self.assertTrue(all(o.status is Status.BUILD_FAILURE for o in failed.outcomes.values()))


if __name__ == "__main__":
    unittest.main()
