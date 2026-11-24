"""The adversarial-review exploits, ported as tests. Each class is one ACT_ON item; every test failed on the
pre-fix code. Real subprocesses over a private copy of tests/fixtures."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import test_invariants as ti  # noqa: E402
from observatory import adapter, catalog, cli, compare, model, report, runner, scoring, store as store_mod  # noqa: E402
from observatory.model import (  # noqa: E402
    AdapterRef, BuildRecord, Cohort, Correction, CorrectionKind, Cost, Incomparable, Mode, Purpose,
    RunManifest, Rubric, ScorerKind, ScoringDecision, Status, Task, Track, Verdict, Disclosure, Split,
)
from observatory.store import Store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures"
PKG = "relfin-mini@0.1.0"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Fx(unittest.TestCase):
    """A fresh fixture copy per test, plus configs written on demand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "fx"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        key = hashlib.sha256(str(Store(self.root / "store").root).encode()).hexdigest()[:8]
        for d in Path(tempfile.gettempdir()).glob(f"obs-{key}-*"):   # scratch an aborted run left for resume
            shutil.rmtree(d)
        self._tmp.cleanup()

    def config(self, name: str, adapter_file: str = "fake.py", settings: dict | None = None,
               command: list[str] | None = None) -> None:
        cmd = command or ["python3", f"adapters/{adapter_file}"]
        lines = ['method_id = "baseline"', 'method_version = "1.0"', 'model_id = "m"', 'model_version = "1"',
                 'ladder_level = "schema_only"', 'information_access = ["schema"]',
                 f"adapter_command = {json.dumps(cmd)}", 'adapter_version = "x"', "", "[settings]"]
        lines += [f"{k} = {json.dumps(v)}" for k, v in (settings or {}).items()]
        (self.root / "catalog" / "configs" / f"{name}.toml").write_text("\n".join(lines) + "\n")

    def load(self):
        return catalog.load(self.root / "catalog"), Store(self.root / "store")

    def run_config(self, config_id: str, evaluator: str = "jay", *, reps: int = 1, score: bool = True,
                   purpose: Purpose = Purpose.SCORED, track: Track = Track.SEMANTIC_TASK):
        cat, st = self.load()
        p = runner.plan(st, cat, config_id=config_id, package=PKG, mode=Mode.CONTROLLED, track=track,
                        purpose=purpose, repetitions=reps)
        rid = runner.execute(st, cat, p, evaluator=evaluator)
        if score:
            scoring.score_run(st, cat, rid)
        return cat, st, p, rid

    def reproduce(self, rid: str, evaluator: str, *, score: bool = True) -> str:
        cat, st = self.load()
        p = runner.reproduce(st, cat, rid, evaluator=evaluator)
        rr = runner.execute(st, cat, p, evaluator=evaluator)
        if score:
            scoring.score_run(st, cat, rr)
        return rr

    def obs(self, *args: str) -> tuple[int, str]:
        base = ["--catalog", str(self.root / "catalog"), "--store", str(self.root / "store")]
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main([*base, *args])
        return code, out.getvalue() + err.getvalue()


class StoredIdentity(Fx):
    """ACT_ON 6: ids are stored with their records and verified on read; shapes are exact."""

    def test_record_with_missing_or_unknown_key_is_refused(self):
        _, st, _, rid = self.run_config("baseline-m1")
        log = st.root / "runs" / rid / "attempts.jsonl"
        good = log.read_text()
        first = json.loads(good.splitlines()[0])
        for broken in ({k: v for k, v in first.items() if k != "schema_version"},
                       {**first, "cost": {k: v for k, v in first["cost"].items() if k != "measured"}},
                       {**first, "surprise": 1}):
            log.write_text(json.dumps(broken) + "\n" + good)
            with self.assertRaises(ValueError):
                st.run(rid)

    def test_edited_manifest_fails_its_stored_id(self):
        _, st, _, rid = self.run_config("baseline-m1")
        f = st.root / "runs" / rid / "manifest.json"
        f.write_text(f.read_text().replace('"t1,t2,t3,t4,t5"', '""'))
        with self.assertRaises(ValueError):
            st.run(rid)

    def test_union_field_comes_back_as_the_arm_it_was(self):
        t = Task("r", "g", frozenset({Track.CONSTRUCTION}), frozenset(), Split.HELDOUT, Disclosure.PUBLIC, "e", "l",
                 "in.json", None, Rubric("why", (0.0, 1.0)))
        self.assertEqual(store_mod._from(Task, store_mod.to_jsonable(t)), t)


class AdapterSeesNoPrivatePaths(Fx):
    """ACT_ON 1: the adapter is handed staged inputs and scratch dirs, never package or store paths."""

    def test_cheat_cannot_find_references_or_the_ledger(self):
        self.config("cheat-m1", "cheat.py")
        cat, st, _, rid = self.run_config("cheat-m1")
        r = st.run(rid)
        self.assertEqual({o.credit for o in r.outcomes.values()}, {0.0})
        roots = (cat.root.parent.resolve(), st.root.resolve())
        for a in r.attempts:
            seen = json.loads((st.root / a.output_ref).read_text())["seen"]
            for p in seen:
                self.assertFalse(any(Path(p).resolve().is_relative_to(x) for x in roots), p)
        self.assertIn(("filesystem", "not_enforced"), r.manifest.isolation)


class NoRerollUntilFavourable(Fx):
    """ACT_ON 2: one run per plan, adapter-reported failures are final, crashes consume the attempt,
    and voids are visible and confirmed by a second person."""

    def test_same_plan_under_another_evaluator_resumes(self):
        cat, st, p, r1 = self.run_config("baseline-m1", "ana")
        r2 = runner.execute(st, cat, p, evaluator="bob")
        self.assertEqual(r1, r2)
        (cohort,) = compare.cohorts(st, cat).values()
        self.assertEqual(len(cohort.runs["baseline-m1"]), 1)

    def test_adapter_reported_failure_is_final(self):
        self.config("reroll-m1", "reroll.py")
        _, st, _, rid = self.run_config("reroll-m1")
        r = st.run(rid)
        self.assertTrue(all(a.attempt_no == 1 for a in r.attempts))
        self.assertEqual({o.status for o in r.outcomes.values()}, {Status.RUNTIME_FAILURE})

    def test_crash_mid_attempt_is_interrupted_and_consumes_the_attempt(self):
        cat, st = self.load()
        p = runner.plan(st, cat, config_id="baseline-m1", package=PKG, mode=Mode.CONTROLLED, repetitions=1)
        st.create_run(RunManifest(p, NOW, "jay", (), ()))
        st.write_build(BuildRecord(p.plan_id, None, "", 0.0, Cost()))
        st.append_start(model.AttemptStart(p.plan_id, "t1", 0, 1, NOW))
        rid = runner.execute(st, cat, p, evaluator="jay")
        t1 = [(a.attempt_no, a.failure) for a in st.run(rid).attempts if a.task_id == "t1"]
        self.assertEqual(t1, [(1, Status.INTERRUPTED), (2, None)])

    def test_void_states_a_class_is_confirmed_by_someone_else_and_stays_visible(self):
        cat, st, _, rid = self.run_config("baseline-m1")
        with self.assertRaises(ValueError):
            Correction(NOW, "jay", CorrectionKind.VOID_RUN, rid, "flaky")
        st.append_correction(Correction(NOW, "jay", CorrectionKind.VOID_RUN, rid, "flaky", model.VoidReason.INFRASTRUCTURE))
        self.assertTrue(any("void" in b.what and rid in b.what for b in report.blockers(st, cat)))
        with self.assertRaises(ValueError):
            st.append_correction(Correction(NOW, "jay", CorrectionKind.CONFIRM_VOID, rid, "me too"))
        st.append_correction(Correction(NOW, "ana", CorrectionKind.CONFIRM_VOID, rid, "saw the outage"))
        self.assertFalse(any("void" in b.what for b in report.blockers(st, cat)))
        index = (report.build(st, cat, self.root / "site") / "index.html").read_text()
        voided = index.split("Voided runs", 1)[1]
        self.assertIn(rid, voided)
        self.assertIn("infrastructure", voided)
        self.assertIn("0.167", voided)


class RelativeRoots(Fx):
    """ACT_ON 7: relative roots work; an adapter that cannot start is a harness error, not a candidate failure."""

    def test_relative_catalog_and_store(self):
        cwd = os.getcwd()
        os.chdir(self.root.parent)
        try:
            cat, st = catalog.load(Path("fx/catalog")), Store(Path("fx/store"))
            p = runner.plan(st, cat, config_id="baseline-m1", package=PKG, mode=Mode.CONTROLLED, repetitions=1)
            rid = runner.execute(st, cat, p, evaluator="jay")
            self.assertNotIn(Status.BUILD_FAILURE, {o.status for o in st.run(rid).outcomes.values()})
        finally:
            os.chdir(cwd)

    def test_unstartable_adapter_aborts_execute(self):
        self.config("ghost-m1", command=["obs-no-such-binary"])
        cat, st = self.load()
        p = runner.plan(st, cat, config_id="ghost-m1", package=PKG, mode=Mode.CONTROLLED, repetitions=1)
        with self.assertRaises(adapter.AdapterNotStartable):
            runner.execute(st, cat, p, evaluator="jay")
        self.assertIsNone(st.run(p.plan_id).build)


class EditedConfig(unittest.TestCase):
    """ACT_ON 3: two identities under one config id never pool."""

    def test_mixed_identity_hashes_are_incomparable(self):
        a = ti.run(ti.config("a"), ti.ALL)
        edited = ti.run(replace(ti.config("a"), settings=(("wrong_tasks", ""),)), ti.ALL)
        with self.assertRaises(Incomparable):
            Cohort(a.manifest.plan.key, ti.PKG, {"a": (a, edited)})


class Adjudication(Fx):
    """ACT_ON 4: adjudication settles live disputes only, by a third party; reviews respect the task's scorer."""

    def test_adjudication_needs_a_dispute_and_a_third_party(self):
        cat, st, _, rid = self.run_config("baseline-m1")
        oid = f"{rid}/t1/0"
        with self.assertRaises(ValueError):   # no dispute: a solo override
            scoring.record_review(st, cat, outcome_id=oid, reviewer="vendor", status=Status.SCORED,
                                  verdict=Verdict(1.0), rationale="x", adjudicate=True)
        scoring.record_review(st, cat, outcome_id=oid, reviewer="ana", status=Status.SCORED, verdict=Verdict(1.0),
                              rationale="answer is fine")
        with self.assertRaises(ValueError):   # party to the dispute
            scoring.record_review(st, cat, outcome_id=oid, reviewer="ana", status=Status.SCORED,
                                  verdict=Verdict(1.0), rationale="x", adjudicate=True)
        scoring.record_review(st, cat, outcome_id=oid, reviewer="lee", status=Status.SCORED, verdict=Verdict(0.5),
                              rationale="partly", adjudicate=True)
        self.assertEqual(st.run(rid).outcomes[("t1", 0)].credit, 0.5)

    def test_review_cannot_preempt_the_executable_check(self):
        cat, st, _, rid = self.run_config("baseline-m1", score=False)
        with self.assertRaises(ValueError):
            scoring.record_review(st, cat, outcome_id=f"{rid}/t1/0", reviewer="ana", status=Status.SCORED,
                                  verdict=Verdict(1.0), rationale="looks right")

    def test_rubric_verdict_must_be_on_its_scale(self):
        cat, st, _, rid = self.run_config("baseline-m1", score=False, track=Track.CONSTRUCTION)
        review = lambda v: scoring.record_review(st, cat, outcome_id=f"{rid}/r1/0", reviewer="ana",
                                                 status=Status.SCORED, verdict=Verdict(v), rationale="judged")
        with self.assertRaises(ValueError):
            review(0.3)
        review(0.5)
        self.assertEqual(st.run(rid).outcomes[("r1", 0)].credit, 0.5)

    def test_decision_for_another_manifest_is_refused(self):
        _, st, _, rid = self.run_config("baseline-m1", score=False)
        with self.assertRaises(ValueError):
            st.append_decision(ScoringDecision(f"{rid}/t1/0", "not-this-manifest", ScorerKind.HUMAN_REVIEW, "ana",
                                               Status.SCORED, Verdict(1.0), "x", (), NOW))


class AdapterNumbers(Fx):
    """ACT_ON 5: every number an adapter reports is validated where it enters."""

    def invoke(self, cost: str | None):
        ref = AdapterRef(("python3", str(self.root / "adapters" / "badcost.py")), "x")
        settings = {} if cost is None else {"cost": cost}
        return adapter.invoke(ref, adapter.Verb.RUN_TASK, {"settings": settings}, artifact_dir=self.root / "a",
                              state_dir=self.root / "s", timeout_s=5, budget=ti.BUDGET, secrets={})

    def test_bad_cost_is_invalid_output(self):
        ok = '{"usd": %s, "tokens_in": %s, "tokens_out": 0, "measured": %s}'
        for bad in (ok % ("NaN", 0, "true"), ok % (-0.5, 0, "true"), ok % ("Infinity", 0, "true"),
                    ok % (0, "1e999", "true"), ok % (0, 1.5, "true"), ok % ("true", 0, "true"), ok % (0, 0, '"yes"')):
            self.assertIs(self.invoke(bad).failure, Status.INVALID_OUTPUT, bad)
        self.assertIsNone(self.invoke(ok % (0.25, 3, "true")).failure)

    def test_missing_cost_is_unmeasured(self):
        r = self.invoke(None)
        self.assertIsNone(r.failure)
        self.assertFalse(r.cost.measured)


class CriticalErrors(unittest.TestCase):
    """ACT_ON 8: a timeout cannot hide a critical error behind a rate."""

    def test_critical_errors_carry_their_denominator(self):
        from observatory.model import Attempt, RunPlan, RunRecord, Seal
        def mk(name, spec):
            plan = RunPlan(ti.config(name), ti.REF, Mode.CONTROLLED, Track.SEMANTIC_TASK, Purpose.SCORED, 1,
                           tuple(t.task_id for t in ti.TASKS), ti.BUDGET)
            m = RunManifest(plan, NOW, "jay", (), ())
            atts, decs = [], []
            for t, s in spec.items():
                timeout = s == "timeout"
                atts.append(Attempt(m.run_id, t, 0, 1, Status.TIMEOUT if timeout else None, None if timeout else "o",
                                    "", 1.0, Cost(), NOW))
                if not timeout:
                    decs.append(ScoringDecision(f"{m.run_id}/{t}/0", m.manifest_id, ScorerKind.EXECUTABLE, "v1",
                                                Status.SCORED, Verdict(0.0 if s == "crit" else 1.0, s == "crit"), "",
                                                (), NOW))
            return RunRecord(m, BuildRecord(m.run_id, None, "", 1.0, Cost()), tuple(atts), (), Seal(m.run_id, NOW, ()),
                             tuple(decs))
        p = mk("p", {"t0": "ok", "t1": "ok", "t2": "ok", "t3": "crit"})
        r = mk("r", {"t0": "ok", "t1": "ok", "t2": "ok", "t3": "timeout"})
        z = mk("z", {t: "timeout" for t in ("t0", "t1", "t2", "t3")})
        c = compare.compare(Cohort(p.manifest.plan.key, ti.PKG, {"p": (p,), "r": (r,), "z": (z,)}))
        got = {s.config_id: (s.critical_errors, s.n_scored, sum(s.status_counts.values())) for s in c.summaries}
        self.assertEqual(got, {"p": (1, 4, 4), "r": (0, 3, 4), "z": (0, 0, 4)})


class ReproductionGate(Fx):
    """ACT_ON 9: every member needs its own scored, verified reproduction by somebody else."""

    def test_reproduction_by_the_original_evaluator_does_not_count(self):
        cat, st, _, rid = self.run_config("baseline-m1", "jay")
        p = runner.reproduce(st, cat, rid, evaluator="ana")
        rr = runner.execute(st, cat, p, evaluator="jay")
        scoring.score_run(st, cat, rr)
        self.assertTrue(any("reproduction" in b.what for b in report.blockers(st, cat)))
        self.assertEqual(scoring.verify(st, cat, rr), [])

    def test_every_member_needs_its_own_reproduction(self):
        self.config("plain-m1")
        cat, st, _, base = self.run_config("baseline-m1")
        self.run_config("plain-m1")
        self.reproduce(base, "ana")
        self.assertEqual([b.what for b in report.blockers(st, cat) if "reproduction" in b.what],
                         ["no qualifying reproduction run of plain-m1 in relfin-mini@0.1.0 controlled semantic_task"])

    def test_unscored_reproduction_does_not_count(self):
        cat, st, _, rid = self.run_config("baseline-m1")
        self.reproduce(rid, "ana", score=False)
        self.assertTrue(any("reproduction" in b.what for b in report.blockers(st, cat)))


class ImposedModel(Fx):
    """ACT_ON 10: controlled mode passes the imposed model and refuses a substitute."""

    def test_model_is_passed_and_echoed(self):
        _, st, _, rid = self.run_config("baseline-m1")
        self.assertEqual({a.model_used for a in st.run(rid).attempts}, {("m", "1")})

    def test_substituted_model_is_a_final_runtime_failure(self):
        self.config("swap-m1", settings={"model_used": "other,9"})
        _, st, _, rid = self.run_config("swap-m1")
        r = st.run(rid)
        self.assertEqual({o.status for o in r.outcomes.values()}, {Status.RUNTIME_FAILURE})
        self.assertEqual({(a.attempt_no, a.model_used) for a in r.attempts}, {(1, ("other", "9"))})


class CheckFaults(Fx):
    """ACT_ON 11: a broken package check is the package's fault, never the candidate's."""

    def check(self, src: str) -> None:
        (self.root / "catalog/packages/relfin-mini/0.1.0/checks/eq.py").write_text(src)

    def test_check_that_raises_leaves_outcomes_unresolved(self):
        self.check("def check(task_input, reference, output):\n    return reference['answr'] == output['answer']\n")
        cat, st, _, rid = self.run_config("baseline-m1", score=False)
        with self.assertRaises(scoring.CheckFault):
            scoring.score_run(st, cat, rid)
        r = st.run(rid)
        self.assertEqual(r.decisions, ())
        self.assertNotIn(Status.INVALID_OUTPUT, {o.status for o in r.outcomes.values()})

    def test_malformed_check_result_crashes_scoring(self):
        self.check("def check(task_input, reference, output):\n    return None\n")
        cat, st, _, rid = self.run_config("baseline-m1", score=False)
        with self.assertRaises(TypeError):
            scoring.score_run(st, cat, rid)
        self.assertEqual(st.run(rid).decisions, ())


class IdsAreNotPaths(Fx):
    """ACT_ON 12: ids are validated where they enter; package refs stay inside the package."""

    def tasks(self, old: str, new: str) -> None:
        f = self.root / "catalog/packages/relfin-mini/0.1.0/tasks.toml"
        f.write_text(f.read_text().replace(old, new, 1))

    def test_catalog_refuses_path_like_ids_and_refs(self):
        prd = Path(__file__).resolve().parents[1] / "docs" / "PRD.md"
        for old, new in (('task_id = "t1"', 'task_id = "../t1"'),
                         ('input_ref = "data/in/t1.json"', f'input_ref = "{prd}"'),
                         ('reference_ref = "data/ref/t1.json"', 'reference_ref = "../../../../../../docs/PRD.md"')):
            shutil.rmtree(self.root)
            shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
            self.tasks(old, new)
            with self.assertRaises(catalog.CatalogError, msg=new):
                catalog.load(self.root / "catalog")

    def test_catalog_refuses_a_check_name_that_only_prefixes_a_function(self):
        (self.root / "catalog/packages/relfin-mini/0.1.0/checks/eq.py").write_text(
            "def check_v2(task_input, reference, output):\n    return None\n")
        with self.assertRaises(catalog.CatalogError):
            catalog.load(self.root / "catalog")

    def test_cli_refuses_path_like_person_ids(self):
        _, st, _, rid = self.run_config("baseline-m1")
        code, _ = self.obs("review", f"{rid}/t1/0", "--value", "0", "--rationale", "x", "--reviewer", "../../../escaped")
        self.assertEqual(code, 1)
        self.assertEqual([p for p in self.root.parent.rglob("escaped*")], [])
        code, _ = self.obs("void", rid, "--reason-class", "harness_bug", "--reason", "x", "--author", "a/b")
        self.assertEqual(code, 1)


class HarnessRobustness(Fx):
    """Consider items: torn log tails, escaped grandchildren, idempotent publish, exploratory subsets."""

    def test_torn_final_line_is_ignored_not_fatal(self):
        _, st, _, rid = self.run_config("baseline-m1")
        with open(st.root / "runs" / rid / "attempts.jsonl", "a") as f:
            f.write('{"run_id": "x", "task_')
        with self.assertWarns(UserWarning):
            self.assertEqual(len(st.run(rid).attempts), 6)

    def test_escaped_grandchild_cannot_stall_invoke(self):
        ref = AdapterRef(("python3", str(self.root / "adapters" / "hang.py")), "x")
        t0 = time.monotonic()
        r = adapter.invoke(ref, adapter.Verb.RUN_TASK, {}, artifact_dir=self.root / "a", state_dir=self.root / "s",
                           timeout_s=1, budget=ti.BUDGET, secrets={})
        self.assertIs(r.failure, Status.TIMEOUT)
        self.assertLess(time.monotonic() - t0, 2.5)

    def test_second_executor_is_refused_while_the_first_works(self):
        import threading
        self.config("slow-m1", settings={"sleep_tasks": "t1,t2,t3,t4,t5,t6", "sleep_s": "0.3"})
        cat, st = self.load()
        p = runner.plan(st, cat, config_id="slow-m1", package=PKG, mode=Mode.CONTROLLED, repetitions=1)
        first = threading.Thread(target=runner.execute, args=(st, cat, p), kwargs={"evaluator": "jay"})
        first.start()
        starts = st.root / "runs" / p.plan_id / "starts.jsonl"
        while first.is_alive() and not starts.exists():
            time.sleep(0.02)
        with self.assertRaisesRegex(RuntimeError, "another process"):
            runner.execute(st, cat, p, evaluator="ana")
        first.join()
        self.assertIsNotNone(st.run(p.plan_id).seal)

    def test_adapter_file_is_part_of_the_identity(self):
        before = self.load()[0].configs["baseline-m1"].identity_hash
        with open(self.root / "adapters" / "fake.py", "a") as f:
            f.write("# quietly tuned\n")
        self.assertNotEqual(self.load()[0].configs["baseline-m1"].identity_hash, before)

    def test_compare_subset_is_labelled_exploratory(self):
        self.run_config("baseline-m1")
        code, out = self.obs("compare", "--configs", "baseline-m1")
        self.assertEqual(code, 0)
        self.assertIn("exploratory", out)
        self.assertNotIn("exploratory", self.obs("compare")[1])


if __name__ == "__main__":
    unittest.main()
