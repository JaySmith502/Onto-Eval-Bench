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
    ExecutableCheck, ScorerKind, ScoringDecision, Status, Verdict, current_decisions,
)
from .store import Store


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
    untrusted data passed to them as a dict. A check that raises on an output
    makes that output INVALID_OUTPUT, with the exception as the rationale.

    write=False returns the decisions without storing them (see verify).
    """
    rec = store.run(run_id)
    plan, mid = rec.manifest.plan, rec.manifest.manifest_id
    pkg = catalog.package(plan.package)
    if pkg.ref != plan.package:
        raise ValueError("the catalog's package differs from the run's (content hash mismatch)")
    pdir, tasks = catalog.package_dir(pkg.ref), {t.task_id: t for t in pkg.tasks}
    final = {}
    for a in rec.attempts:
        final[(a.task_id, a.repetition)] = a
    checks, out = {}, []
    for (task_id, rep), a in sorted(final.items()):
        task = tasks[task_id]
        if a.failure is not None or not isinstance(task.check, ExecutableCheck):
            continue
        fn = checks.get(task_id) or checks.setdefault(task_id, _load_check(pdir, task.check))
        load = lambda ref: json.loads((pdir / ref).read_text("utf-8")) if ref else None
        try:
            res = fn(load(task.input_ref), load(task.reference_ref), json.loads((store.root / a.output_ref).read_text("utf-8")))
            valid, value, critical, note = res.valid, res.value, res.critical_error, str(res.note)
            verdict = Verdict(value, critical) if valid else None
        except Exception as e:  # untrusted output may break a check; that is an invalid output, not a crash
            valid, verdict, note = False, None, f"check raised {type(e).__name__}: {e}"
        d = ScoringDecision(a.outcome_id, mid, ScorerKind.EXECUTABLE, task.check.version,
                            Status.SCORED if valid else Status.INVALID_OUTPUT, verdict, note, (), datetime.now(timezone.utc))
        out.append(d)
        if write:
            store.append_decision(d)
    return out


def verify(store: Store, catalog: Catalog, run_id: str) -> list[str]:
    """Reproduction check: re-score without writing and return outcome ids whose
    recomputed EXECUTABLE decision id differs from the stored one. Empty ==
    executable scoring reproduces exactly."""
    stored: dict[str, set[str]] = {}
    for d in store.run(run_id).decisions:
        if d.kind is ScorerKind.EXECUTABLE:
            stored.setdefault(d.outcome_id, set()).add(d.decision_id)
    fresh = {d.outcome_id: d.decision_id for d in score_run(store, catalog, run_id, write=False)}
    return sorted(o for o in stored.keys() | fresh.keys() if fresh.get(o) not in stored.get(o, ()))


def record_review(store: Store, *, outcome_id: str, reviewer: str, status: Status,
                  verdict: Verdict | None, rationale: str, adjudicate: bool = False) -> ScoringDecision:
    """Human review, or adjudication when `adjudicate`.

    A review never supersedes anything: if it disagrees with the executable
    decision, the outcome becomes UNRESOLVED (a dispute). An adjudication
    supersedes every current decision of the outcome. manifest_id is looked up
    from the run, never supplied by the caller."""
    run_id, rest = outcome_id.split("/", 1)
    task_id, rep = rest.rsplit("/", 1)
    rec = store.run(run_id)
    outcome = rec.outcomes.get((task_id, int(rep)))
    if outcome is None or outcome.status not in (Status.SCORED, Status.INVALID_OUTPUT, Status.UNRESOLVED):
        raise ValueError(f"{outcome_id} has no completed output to review (status {outcome and outcome.status.name})")
    oid = f"{rec.run_id}/{task_id}/{rep}"
    heads = [d.decision_id for d in current_decisions(tuple(d for d in rec.decisions if d.outcome_id == oid))]
    d = ScoringDecision(oid, rec.manifest.manifest_id,
                        ScorerKind.ADJUDICATION if adjudicate else ScorerKind.HUMAN_REVIEW, reviewer, status, verdict,
                        rationale, tuple(heads) if adjudicate else (), datetime.now(timezone.utc))
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
