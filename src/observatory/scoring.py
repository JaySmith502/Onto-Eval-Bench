"""FR-07: executable scoring, human review, adjudication, reproduction check.

Scores exist only as ScoringDecisions. Which decision counts is decided in
one place, model.resolve_outcome, by supersession: a disagreement leaves two
current decisions (UNRESOLVED) until an adjudication supersedes both.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from .catalog import Catalog
from .model import (
    ExecutableCheck, Rubric, ScorerKind, ScoringDecision, Status, Verdict, current_decisions,
)
from .store import Store


class CheckFault(RuntimeError):
    """Package checks raised on some outcomes. No decision was written for them (they stay
    UNRESOLVED); `decisions` holds the ones that were scored."""

    def __init__(self, faults: dict[str, str], decisions: list[ScoringDecision]) -> None:
        super().__init__("checks raised (package bug, not a candidate failure):\n" + "\n".join(
            f"  {oid}: {msg}" for oid, msg in sorted(faults.items())))
        self.faults, self.decisions = faults, decisions


@dataclass(frozen=True)
class CheckResult:
    """What a package check returns:
    `def check(task_input: dict, reference: dict | None, output: dict) -> CheckResult`.
    valid=False means the output could not be interpreted (INVALID_OUTPUT)."""
    valid: bool
    value: float
    critical_error: bool
    note: str


def _load_check(pdir, check: ExecutableCheck):
    rel, _, fn = check.entrypoint.partition(":")
    path = pdir / rel
    if hashlib.sha256(path.read_bytes()).hexdigest() != check.version:
        raise ValueError(f"{path} changed since the catalog was loaded")
    spec = importlib.util.spec_from_file_location(f"_obs_check_{check.version[:12]}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, fn)


def score_run(store: Store, catalog: Catalog, run_id: str, *, write: bool = True) -> list[ScoringDecision]:
    """Apply executable checks to every outcome whose final attempt succeeded.

    Idempotent: decision ids are content hashes, so re-scoring appends nothing
    new. Rubric tasks are left for review. Failed attempts get no decision;
    their credit is 0 by the rule in model.Outcome.credit. Checks are package
    code run in-process (trusted, versioned by hash); adapter output is
    untrusted data passed to them as a dict, so a check must answer valid=False
    for output it cannot interpret. A check that raises is a package bug: that
    outcome gets no decision (UNRESOLVED), and after the other decisions are
    written CheckFault is raised. A check that returns anything but a
    CheckResult raises TypeError before anything is written.

    write=False returns the decisions without storing them (see verify).
    """
    rec = store.run(run_id)
    plan, mid = rec.manifest.plan, rec.manifest.manifest_id
    pkg = catalog.package(plan.package)
    if pkg.ref != plan.package:
        raise ValueError("the catalog's package differs from the run's (content hash mismatch)")
    pdir, tasks = catalog.package_dir(pkg.ref), {t.task_id: t for t in pkg.tasks}
    checks, out, faults = {}, [], {}
    load = lambda ref: json.loads((pdir / ref).read_text("utf-8")) if ref else None
    for (task_id, rep), a in sorted(rec.final_attempts.items()):
        task = tasks[task_id]
        if a.failure is not None or not isinstance(task.check, ExecutableCheck):
            continue
        fn = checks.get(task_id) or checks.setdefault(task_id, _load_check(pdir, task.check))
        args = load(task.input_ref), load(task.reference_ref), json.loads((store.root / a.output_ref).read_text("utf-8"))
        try:
            res = fn(*args)
        except Exception as e:  # the package's bug: never charged to the candidate as INVALID_OUTPUT
            faults[a.outcome_id] = f"{type(e).__name__}: {e}"
            continue
        if not isinstance(res, CheckResult):
            raise TypeError(f"check {task.check.entrypoint} returned {type(res).__name__}, not CheckResult")
        out.append(ScoringDecision(a.outcome_id, mid, ScorerKind.EXECUTABLE, task.check.version,
                                   Status.SCORED if res.valid else Status.INVALID_OUTPUT,
                                   Verdict(res.value, res.critical_error) if res.valid else None, str(res.note), (),
                                   datetime.now(timezone.utc)))
    if write:
        for d in out:
            store.append_decision(d)
    if faults:
        raise CheckFault(faults, out)
    return out


def verify(store: Store, catalog: Catalog, run_id: str) -> list[str]:
    """Reproduction check: re-score without writing and return outcome ids whose
    recomputed EXECUTABLE decision id differs from the stored one (a check that now
    raises counts as differing). Empty == executable scoring reproduces exactly."""
    stored: dict[str, set[str]] = {}
    for d in store.run(run_id).decisions:
        if d.kind is ScorerKind.EXECUTABLE:
            stored.setdefault(d.outcome_id, set()).add(d.decision_id)
    try:
        ds, faulted = score_run(store, catalog, run_id, write=False), set()
    except CheckFault as e:
        ds, faulted = e.decisions, set(e.faults)
    fresh = {d.outcome_id: d.decision_id for d in ds}
    return sorted(faulted | {o for o in stored.keys() | fresh.keys() if fresh.get(o) not in stored.get(o, ())})


def record_review(store: Store, catalog: Catalog, *, outcome_id: str, reviewer: str, status: Status,
                  verdict: Verdict | None, rationale: str, adjudicate: bool = False) -> ScoringDecision:
    """Human review, or adjudication when `adjudicate`.

    A review never supersedes anything: if it disagrees with the executable
    decision, the outcome becomes UNRESOLVED (a dispute). An adjudication
    supersedes every current decision of the outcome, and is refused unless those
    decisions disagree and the adjudicator made none of them. On an executable task a
    review waits for the check's decision; on a rubric task the verdict must be on the
    rubric's scale. manifest_id is looked up from the run, never supplied by the caller."""
    run_id, rest = outcome_id.split("/", 1)
    task_id, rep = rest.rsplit("/", 1)
    rec = store.run(run_id)
    outcome = rec.outcomes.get((task_id, int(rep)))
    if outcome is None or outcome.status not in (Status.SCORED, Status.INVALID_OUTPUT, Status.UNRESOLVED):
        raise ValueError(f"{outcome_id} has no completed output to review (status {outcome and outcome.status.name})")
    oid = f"{rec.run_id}/{task_id}/{rep}"
    mine = tuple(d for d in rec.decisions if d.outcome_id == oid)
    heads = current_decisions(mine)
    check = next(t.check for t in catalog.package(rec.manifest.plan.package).tasks if t.task_id == task_id)
    if isinstance(check, Rubric):
        if verdict is not None and verdict.value not in check.scale:
            raise ValueError(f"{verdict.value} is not on the rubric scale {check.scale}")
    elif not any(d.kind is ScorerKind.EXECUTABLE for d in mine):
        raise ValueError(f"{oid} has no executable decision yet; run `obs score {rec.run_id}` before reviewing")
    if adjudicate:
        if len({(d.status, d.verdict) for d in heads}) < 2:
            raise ValueError(f"{oid} has no dispute to adjudicate; record a review instead")
        if reviewer in {d.scorer_id for d in heads}:
            raise ValueError(f"{reviewer} made a decision in this dispute and cannot adjudicate it")
    d = ScoringDecision(oid, rec.manifest.manifest_id,
                        ScorerKind.ADJUDICATION if adjudicate else ScorerKind.HUMAN_REVIEW, reviewer, status, verdict,
                        rationale, tuple(h.decision_id for h in heads) if adjudicate else (), datetime.now(timezone.utc))
    store.append_decision(d)
    return d


def review_sample(store: Store, run_ids: list[str], *, rate: float, seed: int) -> list[str]:
    """Outcome ids of executable-scored outcomes to send to human review,
    sampled uniformly with a recorded seed. Disagreements surface as disputes;
    their rate is the automated-vs-reviewed agreement metric (PRD section 24)."""
    pool = []
    for rid in run_ids:
        rec = store.run(rid)
        for (task_id, rep), o in sorted(rec.outcomes.items()):
            ds = [d for d in rec.decisions if d.outcome_id == f"{rec.run_id}/{task_id}/{rep}"]
            if o.status in (Status.SCORED, Status.INVALID_OUTPUT) and ds and all(d.kind is ScorerKind.EXECUTABLE for d in ds):
                pool.append(f"{rec.run_id}/{task_id}/{rep}")
    return sorted(random.Random(seed).sample(pool, min(len(pool), math.ceil(rate * len(pool)))))
