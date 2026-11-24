"""`obs` command. Thin shell: parse args, call one library function, print.

    obs check                                   catalog.load; print every problem
    obs plan  CONFIG PKG@VER --mode M [--track T] [--smoke] [--reps N]  -> plan file, or prints the persisted incompatibility
    obs run   PLAN_FILE                         runner.execute (resumable)
    obs intervene RUN --kind K --minutes M [--task T] --note "..."
    obs score RUN                               scoring.score_run
    obs review-sample RUN... --rate R --seed S  print outcome ids to review
    obs review OUTCOME (--value V | --invalid) --rationale "..." [--adjudicate] [--critical]
    obs verify RUN                              scoring.verify (reproduction check)
    obs compare [--configs A,B] [--tags T]      one table per cohort, never merged
    obs void RUN --reason "..."                 Correction(VOID_RUN)
    obs reproduce RUN                           runner.reproduce -> plan file
    obs report OUT_DIR                          draft site + export
    obs publish                                 refuses while blockers exist; prints them
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import catalog as catalog_mod
from . import compare, report, runner, scoring
from .model import (
    Correction, CorrectionKind, Incompatibility, Intervention, InterventionKind, Mode, Purpose, Status, Track, Verdict,
)
from .store import Store


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="obs")
    ap.add_argument("--catalog", default="catalog", type=Path)
    ap.add_argument("--store", default="store", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, *args: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        for a in args:
            p.add_argument(a, nargs="+" if a == "runs" else None)
        return p

    cmd("check")
    p = cmd("plan", "config", "package")
    p.add_argument("--mode", required=True, choices=[m.value for m in Mode])
    p.add_argument("--track", default=Track.SEMANTIC_TASK.value, choices=[t.value for t in Track])
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--evaluator", default=getpass.getuser())
    p.add_argument("--out", type=Path)
    p = cmd("run", "plan_file")
    p.add_argument("--evaluator")
    p = cmd("intervene", "run")
    p.add_argument("--kind", required=True, choices=[k.value for k in InterventionKind])
    p.add_argument("--minutes", type=float, required=True)
    p.add_argument("--task")
    p.add_argument("--note", required=True)
    p.add_argument("--actor", default=getpass.getuser())
    cmd("score", "run")
    p = cmd("review-sample", "runs")
    p.add_argument("--rate", type=float, required=True)
    p.add_argument("--seed", type=int, required=True)
    p = cmd("review", "outcome")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--value", type=float)
    g.add_argument("--invalid", action="store_true")
    p.add_argument("--rationale", required=True)
    p.add_argument("--adjudicate", action="store_true")
    p.add_argument("--critical", action="store_true")
    p.add_argument("--reviewer", default=getpass.getuser())
    cmd("verify", "run")
    p = cmd("compare")
    p.add_argument("--configs")
    p.add_argument("--tags")
    p = cmd("void", "run")
    p.add_argument("--reason", required=True)
    p.add_argument("--author", default=getpass.getuser())
    p = cmd("reproduce", "run")
    p.add_argument("--evaluator", required=True)
    p.add_argument("--out", type=Path)
    cmd("report", "out_dir")
    p = cmd("publish")
    p.add_argument("--editor", default=getpass.getuser())
    return ap


def _write_plan(out: Path | None, plan: object, evaluator: str) -> Path:
    path = out or Path("plans") / f"{plan.plan_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # The plan is re-derived from the catalog by `obs run`; the file pins its id.
    path.write_text(json.dumps({"plan_id": plan.plan_id, "config_id": plan.config.config_id, "package": str(plan.package),
                                "mode": plan.mode.value, "track": plan.track.value, "purpose": plan.purpose.value,
                                "repetitions": plan.repetitions, "evaluator": evaluator}, indent=1) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    store = Store(a.store)
    try:
        if a.cmd == "check":
            cat = catalog_mod.load(a.catalog)
            print(f"ok: {len(cat.candidates)} candidates, {len(cat.configs)} configs, {len(cat.packages)} packages")
            return 0
        cat = catalog_mod.load(a.catalog)
        return _dispatch(a, store, cat)
    except catalog_mod.CatalogError as e:
        print("catalog problems:\n" + "\n".join(f"  - {p}" for p in e.problems), file=sys.stderr)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
    return 1


def _dispatch(a: argparse.Namespace, store: Store, cat: catalog_mod.Catalog) -> int:
    if a.cmd in ("plan", "reproduce"):
        if a.cmd == "plan":
            p = runner.plan(store, cat, config_id=a.config, package=a.package, mode=Mode(a.mode), track=Track(a.track),
                            purpose=Purpose.SMOKE if a.smoke else Purpose.SCORED, repetitions=a.reps)
        else:
            p = runner.reproduce(store, cat, a.run, evaluator=a.evaluator)
        if isinstance(p, Incompatibility):
            print("incompatible (recorded): " + "; ".join(p.reasons))
            return 1
        print(_write_plan(a.out, p, a.evaluator), p.plan_id)
    elif a.cmd == "run":
        f = json.loads(Path(a.plan_file).read_text())
        p = runner.plan(store, cat, config_id=f["config_id"], package=f["package"], mode=Mode(f["mode"]),
                        track=Track(f["track"]), purpose=Purpose(f["purpose"]), repetitions=f["repetitions"])
        if isinstance(p, Incompatibility) or p.plan_id != f["plan_id"]:
            raise ValueError("the catalog no longer reproduces this plan file")
        print(runner.execute(store, cat, p, evaluator=a.evaluator or f["evaluator"]))
    elif a.cmd == "intervene":
        run = store.run(a.run)
        store.append_intervention(Intervention(run.run_id, datetime.now(timezone.utc), a.actor, InterventionKind(a.kind),
                                               a.minutes, a.task, a.note))
    elif a.cmd == "score":
        ds = scoring.score_run(store, cat, a.run)
        print(f"{len(ds)} executable decisions")
    elif a.cmd == "review-sample":
        print("\n".join(scoring.review_sample(store, a.runs, rate=a.rate, seed=a.seed)))
    elif a.cmd == "review":
        d = scoring.record_review(store, outcome_id=a.outcome, reviewer=a.reviewer,
                                  status=Status.INVALID_OUTPUT if a.invalid else Status.SCORED,
                                  verdict=None if a.invalid else Verdict(a.value, a.critical), rationale=a.rationale,
                                  adjudicate=a.adjudicate)
        print(d.decision_id)
    elif a.cmd == "verify":
        bad = scoring.verify(store, cat, a.run)
        print("\n".join(bad) if bad else "reproduces exactly")
        return 1 if bad else 0
    elif a.cmd == "compare":
        want = a.configs.split(",") if a.configs else None
        tags = frozenset(a.tags.split(",")) if a.tags else frozenset()
        for key, cohort in compare.cohorts(store, cat).items():
            members = [c for c in want if c in cohort.runs] if want else None
            if members == []:
                continue
            c = compare.compare(cohort, members, tags)
            print(f"== {key.package} / {key.mode} / {key.track}" + (f" / model {key.model[0]} {key.model[1]}" if key.model else ""))
            print(f"   {c.population.note}; {c.population.n_groups} groups; unresolved {len(c.unresolved)}")
            for s in c.summaries:
                counts = " ".join(f"{k.name}={v}" for k, v in sorted(s.status_counts.items(), key=lambda kv: kv[0].name))
                print(f"   {s.config_id}: {s.score.estimate:.3f} [{s.score.low:.3f}, {s.score.high:.3f}] ({counts})")
            for p in c.pairs:
                print(f"   {p.a} vs {p.b}: diff {p.diff.estimate:+.3f} [{p.diff.low:+.3f}, {p.diff.high:+.3f}] {p.call.value}")
    elif a.cmd == "void":
        run = store.run(a.run)
        store.append_correction(Correction(datetime.now(timezone.utc), a.author, CorrectionKind.VOID_RUN, run.run_id, a.reason))
    elif a.cmd == "report":
        print(report.build(store, cat, Path(a.out_dir)))
    elif a.cmd == "publish":
        try:
            print(report.publish(store, cat, editor=a.editor))
        except report.PublishBlocked as e:
            print("publication refused:", file=sys.stderr)
            for b in e.blockers:
                print(f"  - {b.what}  (fix: {b.fix})", file=sys.stderr)
            return 1
    return 0
