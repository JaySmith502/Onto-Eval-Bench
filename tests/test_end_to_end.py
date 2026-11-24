"""plan -> execute -> score -> compare -> report through the library, plus one pass through cli.main,
over the tiny fixture package with a fake adapter. Slow-ish (real subprocesses, one real timeout)."""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from observatory import cli, compare, catalog, report, runner, scoring  # noqa: E402
from observatory.model import Attempt, AttemptStart, Cost, Mode, Purpose, RunManifest, Status, Verdict  # noqa: E402
from observatory.store import AppendOnlyViolation, Store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures"
PKG = "relfin-mini@0.1.0"


def run_all(store, cat, config_id, mode, evaluator, reps, purpose=Purpose.SCORED):
    p = runner.plan(store, cat, config_id=config_id, package=PKG, mode=mode, purpose=purpose, repetitions=reps)
    run_id = runner.execute(store, cat, p, evaluator=evaluator)
    scoring.score_run(store, cat, run_id)
    return p, run_id


class Library(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "fx"
        shutil.copytree(FIXTURE, root)
        cls.cat = catalog.load(root / "catalog")
        cls.store = Store(root / "store")
        cls.plan_a, cls.a = run_all(cls.store, cls.cat, "ontoA-m1", Mode.CONTROLLED, "jay", 1)
        cls.plan_b, cls.b = run_all(cls.store, cls.cat, "baseline-m1", Mode.CONTROLLED, "jay", 2)
        cls.plan_n, cls.n = run_all(cls.store, cls.cat, "ontoA-native", Mode.NATIVE, "jay", 1)
        for orig in (cls.a, cls.b, cls.n):
            p = runner.reproduce(cls.store, cls.cat, orig, evaluator="ana")
            cls.rid = runner.execute(cls.store, cls.cat, p, evaluator="ana")
            scoring.score_run(cls.store, cls.cat, cls.rid)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outcomes_are_dense_and_failures_are_zero_credit(self):
        for r in self.store.runs():
            p = r.manifest.plan
            self.assertEqual(len(r.outcomes), len(p.task_ids) * p.repetitions)
            self.assertNotIn(Status.MISSING, {o.status for o in r.outcomes.values()})
        o = self.store.run(self.a).outcomes
        self.assertEqual((o[("t4", 0)].status, o[("t4", 0)].credit), (Status.TIMEOUT, 0.0))
        self.assertEqual(o[("t5", 0)].status, Status.RUNTIME_FAILURE)
        self.assertEqual((o[("t1", 0)].status, o[("t1", 0)].credit), (Status.SCORED, 1.0))
        self.assertEqual((o[("t2", 0)].status, o[("t2", 0)].credit), (Status.SCORED, 0.0))
        self.assertEqual(sum(a.task_id == "t4" for a in self.store.run(self.a).attempts), 2)  # retried once

    def test_execute_is_idempotent(self):
        before = self.store.run(self.a)
        self.assertEqual(runner.execute(self.store, self.cat, self.plan_a, evaluator="jay"), self.a)
        after = self.store.run(self.a)
        self.assertEqual(before.attempts, after.attempts)
        self.assertEqual(before.seal, after.seal)

    def test_store_rejects_out_of_plan_attempts(self):
        m = self.store.run(self.a).manifest
        st = Store(Path(self.tmp.name) / "other-store")   # an open run kept out of the shared store's cohorts
        st.create_run(m)
        now = datetime.now(timezone.utc)
        start = lambda task, rep, n, run=m.run_id: AttemptStart(run, task, rep, n, now)
        mk = lambda task, rep, n, failure=None: Attempt(m.run_id, task, rep, n, failure, None, "", 0.1, Cost(), now)
        for bad in (start("nope", 0, 1), start("t1", 5, 1), start("t1", 0, 2)):
            with self.assertRaises(AppendOnlyViolation):
                st.append_start(bad)
        with self.assertRaises(AppendOnlyViolation):      # an attempt needs its start
            st.append_attempt(mk("t1", 0, 1))
        st.append_start(start("t1", 0, 1))
        with self.assertRaises(AppendOnlyViolation):      # one attempt in flight at a time
            st.append_start(start("t2", 0, 1))
        st.append_attempt(mk("t1", 0, 1, Status.TIMEOUT))
        st.append_start(start("t1", 0, 2))                 # TIMEOUT is retryable
        st.append_attempt(mk("t1", 0, 2, Status.TIMEOUT))
        with self.assertRaises(AppendOnlyViolation):      # max_attempts is 2
            st.append_start(start("t1", 0, 3))
        st.append_start(start("t2", 0, 1))
        st.append_attempt(mk("t2", 0, 1))
        with self.assertRaises(AppendOnlyViolation):      # a completed answer is never re-sampled
            st.append_start(start("t2", 0, 2))
        with self.assertRaises(AppendOnlyViolation):      # sealed run
            self.store.append_start(start("t2", 0, 1, run=self.a))

    def test_controlled_and_native_are_different_cohorts(self):
        cs = compare.cohorts(self.store, self.cat)
        self.assertEqual({(k.mode, k.model) for k in cs}, {(Mode.CONTROLLED, ("m", "1")), (Mode.NATIVE, None)})
        controlled = next(c for k, c in cs.items() if k.mode is Mode.CONTROLLED)
        self.assertEqual(set(controlled.runs), {"ontoA-m1", "baseline-m1"})
        c = compare.compare(controlled)
        score = {s.config_id: s.score.estimate for s in c.summaries}
        self.assertAlmostEqual(score["ontoA-m1"], 0.5)
        self.assertAlmostEqual(score["baseline-m1"], 1 / 6)
        self.assertEqual(len(c.pairs), 1)
        self.assertEqual(c.population.n_groups, 3)

    def test_smoke_plan_is_config_split_only(self):
        p = runner.plan(self.store, self.cat, config_id="baseline-m1", package=PKG, mode=Mode.CONTROLLED,
                        purpose=Purpose.SMOKE)
        self.assertEqual(p.task_ids, ("c1",))

    def test_wrong_model_in_controlled_mode_is_an_incompatibility(self):
        inc = runner.plan(self.store, self.cat, config_id="ontoA-native", package=PKG, mode=Mode.CONTROLLED)
        self.assertEqual(inc.config_id, "ontoA-native")
        self.assertIn(inc, self.store.incompatibilities())

    def test_verify_reproduces_exactly(self):
        for r in self.store.runs():
            self.assertEqual(scoring.verify(self.store, self.cat, r.run_id), [])

    def test_export_redacts_restricted_tasks(self):
        out = report.export(self.store, self.cat, Path(self.tmp.name) / "export")
        text = (out / "bundle.json").read_text()
        self.assertIn("SECRET-REF-t1", text)
        self.assertIn(f"{self.a}/t6/0", text)               # restricted ids, statuses and scores stay
        for leak in ("SECRET-REF-t6", "SECRET-IN-t6"):
            self.assertNotIn(leak, text)

    def test_publish_refuses_while_unresolved_then_succeeds_after_adjudication(self):
        self.assertEqual(report.blockers(self.store, self.cat), [])
        oid = f"{self.a}/t1/0"
        scoring.record_review(self.store, self.cat, outcome_id=oid, reviewer="ana", status=Status.SCORED, verdict=Verdict(0.0),
                              rationale="wrong join")
        self.assertEqual(self.store.run(self.a).outcomes[("t1", 0)].status, Status.UNRESOLVED)
        self.assertTrue(any("unresolved" in b.what and oid in b.what for b in report.blockers(self.store, self.cat)))
        with self.assertRaises(report.PublishBlocked):
            report.publish(self.store, self.cat, editor="lee")
        scoring.record_review(self.store, self.cat, outcome_id=oid, reviewer="lee", status=Status.SCORED, verdict=Verdict(0.5),
                              rationale="partially right", adjudicate=True)
        self.assertEqual(self.store.run(self.a).outcomes[("t1", 0)].credit, 0.5)
        self.assertEqual(report.blockers(self.store, self.cat), [])
        site = report.publish(self.store, self.cat, editor="lee")
        self.assertTrue((site / "index.html").exists() and (site / "export" / "bundle.json").exists())
        self.assertEqual(report.publish(self.store, self.cat, editor="lee"), site)   # same evidence, same publication
        self.assertNotIn("DRAFT", (site / "index.html").read_text())
        draft = report.build(self.store, self.cat, Path(self.tmp.name) / "draft")
        self.assertIn("DRAFT", (draft / "index.html").read_text())


class Cli(unittest.TestCase):
    def test_one_pass_through_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fx"
            shutil.copytree(FIXTURE, root)
            base = ["--catalog", str(root / "catalog"), "--store", str(root / "store")]
            plan_file = str(root / "plan.json")

            def obs(*args):
                with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                    code = cli.main([*base, *args])
                return code, out.getvalue()

            self.assertEqual(obs("check")[0], 0)
            self.assertEqual(obs("plan", "ontoA-native", PKG, "--mode", "controlled")[0], 1)   # incompatible, recorded
            self.assertEqual(obs("plan", "baseline-m1", PKG, "--mode", "controlled", "--reps", "1",
                                 "--evaluator", "jay", "--out", plan_file)[0], 0)
            self.assertEqual(obs("run", plan_file)[0], 0)
            run_id = next((root / "store" / "runs").iterdir()).name
            self.assertEqual(obs("score", run_id)[0], 0)
            self.assertEqual(obs("verify", run_id)[0], 0)
            code, out = obs("compare")
            self.assertEqual(code, 0)
            self.assertIn("baseline-m1", out)
            self.assertEqual(obs("report", str(root / "site"))[0], 0)
            self.assertTrue((root / "site" / "index.html").exists())
            self.assertEqual(obs("publish")[0], 1)                      # no reproduction run yet
            self.assertEqual(len(Store(root / "store").incompatibilities()), 1)


if __name__ == "__main__":
    unittest.main()
