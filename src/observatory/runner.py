"""Plan (pre-register) and execute runs. Owns retries, spend caps, resumption.

Two public functions do the work: `plan` and `execute`. `reproduce` is `plan`
seeded from an existing manifest.
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .adapter import Verb, invoke, isolation
from .catalog import Catalog
from .model import (
    AdapterRef, Attempt, AttemptStart, BuildRecord, Cost, Incompatibility, Mode, Purpose, RETRYABLE, RunManifest, RunPlan, Seal,
    Status, Track,
)
from .store import Store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def plan(store: Store, catalog: Catalog, *, config_id: str, package: str, mode: Mode,
         track: Track = Track.SEMANTIC_TASK, purpose: Purpose = Purpose.SCORED,
         repetitions: int = 3) -> RunPlan | Incompatibility:
    """Pre-register a run. Writes nothing except a returned Incompatibility,
    which it persists itself so no caller can forget to.

    Invariants of the returned RunPlan:
    - task_ids == package.eligible(track, plan.split). Callers cannot pass a task list.
    - budget == package.budget(mode). Callers cannot pass a budget.
    - repetitions >= 1.

    Returns (and persists) an Incompatibility when mode is CONTROLLED and the
    config's (model_id, model_version) != package.controlled_model (PRD §7:
    report, don't substitute). Claims never change the plan: a candidate that
    claims a capability unsupported is still planned on every eligible task.
    """
    cfg, pkg = catalog.configs[config_id], catalog.package(package)
    if mode is Mode.CONTROLLED and (cfg.model_id, cfg.model_version) != pkg.controlled_model:
        inc = Incompatibility(config_id, pkg.ref, mode, track, (
            f"controlled mode imposes model {pkg.controlled_model}; config uses {(cfg.model_id, cfg.model_version)}",))
        store.append_incompatibility(inc)
        return inc
    p = RunPlan(cfg, pkg.ref, mode, track, purpose, repetitions, (), pkg.budget(mode))
    tasks = tuple(t.task_id for t in pkg.eligible(track, p.split))
    if not tasks:
        raise ValueError(f"{pkg.ref} has no eligible {p.split} tasks on track {track}")
    return replace(p, task_ids=tasks)


def _abs(adapter: AdapterRef, base: Path) -> AdapterRef:
    """Command arguments naming a file under `base` become absolute; the identity keeps them relative."""
    return replace(adapter, command=tuple(str(base / a) if (base / a).is_file() else a for a in adapter.command))


def _sum(rs) -> Cost:
    cs = [r.cost for r in rs]
    return Cost(sum(c.usd for c in cs), sum(c.tokens_in for c in cs), sum(c.tokens_out for c in cs), all(c.measured for c in cs))


def execute(store: Store, catalog: Catalog, plan: RunPlan, *, evaluator: str,
            secrets: dict[str, str] | None = None) -> str:
    """Execute a plan; return run_id. Resumable and idempotent.

    Every (task, repetition) in the plan ends with exactly one final attempt in
    attempts.jsonl, so the outcome matrix is dense by construction.

    The run id is the plan_id: re-executing a plan, by anyone, resumes or returns the
    sealed run and never starts a second sampling. `evaluator` is recorded when the run
    is created. Each attempt is preceded by a start record; a start a crash left open is
    resolved to INTERRUPTED on resume and spends that attempt. Order of cells is shuffled
    with a seed from plan_id, so a mid-run spend cap doesn't systematically starve the
    same tail of the task list.
    """
    pkg = catalog.package(plan.package)
    if pkg.ref != plan.package:
        raise ValueError("the catalog's package differs from the plan's (content hash mismatch)")
    if plan.task_ids != tuple(t.task_id for t in pkg.eligible(plan.track, plan.split)) or plan.budget != pkg.budget(plan.mode):
        raise ValueError("plan does not match its package's eligible tasks and budget")
    b, run_id = plan.budget, plan.plan_id
    if not store.exists(run_id):
        env = (("python", sys.version.split()[0]), ("platform", platform.platform()),
               ("adapter_version_declared", plan.config.adapter.version))
        store.create_run(RunManifest(plan, _now(), evaluator, env, isolation(b, plan.config.adapter)))
    declared = plan.config.adapter.secret_env
    if missing := [n for n in declared if n not in (secrets or {})]:
        raise ValueError(f"config {plan.config.config_id} needs secret_env {missing}; pass them to execute")
    secrets = {n: secrets[n] for n in declared}   # only what the config names reaches the adapter
    with store.run_lock(run_id):
        rec = store.run(run_id)
        if rec.seal:
            return run_id

        # Adapter-owned dirs live outside catalog and store, keyed by store and run so nothing is shared.
        scratch = Path(tempfile.gettempdir()) / f"obs-{hashlib.sha256(str(store.root).encode()).hexdigest()[:8]}-{run_id}"
        state = scratch / "state"
        if rec.build is None:
            shutil.rmtree(scratch, ignore_errors=True)
        adapter, settings = _abs(plan.config.adapter, catalog.root.parent), dict(plan.config.settings)
        imposed = plan.key.model
        common = {"state_dir": str(state)} | ({"model": list(imposed)} if imposed else {})

        def call(verb: Verb, req: dict, timeout: float, name: str, rep: int = 0, n: int = 0):
            return invoke(adapter, verb, {**common, **req}, artifact_dir=store.artifact_dir(run_id, name, rep, n),
                          state_dir=state, timeout_s=timeout, budget=b, secrets=secrets)

        # setup runs on every execute, resumes included, so adapters must make it idempotent
        setups = store.artifact_dir(run_id, "_setup", 0, 0).parent
        setup = call(Verb.SETUP, {"settings": settings}, b.build_timeout_s, "_setup", 0,
                     len(list(setups.iterdir())) if setups.exists() else 0)
        build = rec.build
        if build is None:
            rs, staged, pdir = [setup], scratch / "inputs", catalog.package_dir(pkg.ref)
            if setup.failure is None:
                for d in pkg.data:
                    (staged / d.path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(pdir / d.path, staged / d.path)
                rs.append(call(Verb.BUILD, {"settings": settings, "inputs": [str(staged / d.path) for d in pkg.data],
                                            "information_access": sorted(plan.config.information_access)},
                               b.build_timeout_s, "_build"))
            f = rs[-1].failure
            build = BuildRecord(run_id, None if f is None else Status.TIMEOUT if f is Status.TIMEOUT else Status.BUILD_FAILURE,
                                rs[-1].error, sum(r.wall_s for r in rs), _sum(rs))
            store.write_build(build)

        if build.failure is None:
            tasks = {t.task_id: t for t in pkg.tasks}
            inputs = {tid: json.loads((catalog.package_dir(pkg.ref) / tasks[tid].input_ref).read_text("utf-8"))
                      for tid in plan.task_ids}
            last = dict(rec.final_attempts)
            for s in store.open_starts(run_id):
                last[(s.task_id, s.repetition)] = att = Attempt(
                    run_id, s.task_id, s.repetition, s.attempt_no, Status.INTERRUPTED, None, "harness stopped mid-attempt",
                    0.0, Cost(measured=False), _now())
                store.append_attempt(att)
            spent = build.cost.usd + sum(a.cost.usd for a in rec.attempts)
            cells = [(t, r) for t in plan.task_ids for r in range(plan.repetitions)]
            random.Random(plan.plan_id).shuffle(cells)
            for task_id, rep in cells:
                while True:
                    prev = last.get((task_id, rep))
                    if prev is not None and (prev.failure not in RETRYABLE or prev.attempt_no >= b.max_attempts):
                        break
                    n = prev.attempt_no + 1 if prev else 1
                    store.append_start(AttemptStart(run_id, task_id, rep, n, _now()))
                    if spent >= b.max_usd:
                        att = Attempt(run_id, task_id, rep, n, Status.BUDGET_EXHAUSTED, None, "spending limit reached",
                                      0.0, Cost(), _now())
                    else:
                        r = call(Verb.RUN_TASK, {"settings": settings, "task_id": task_id, "input": inputs[task_id]},
                                 b.task_timeout_s, task_id, rep, n)
                        failure, error, ref = r.failure, r.error, None
                        if failure is None and imposed and r.model_used != imposed:
                            failure, error = Status.RUNTIME_FAILURE, f"controlled mode imposes {imposed}; adapter used {r.model_used}"
                        if failure is None:
                            out = store.artifact_dir(run_id, task_id, rep, n) / "output.json"
                            out.write_text(json.dumps(r.output, sort_keys=True), "utf-8")
                            ref = out.relative_to(store.root).as_posix()
                        att = Attempt(run_id, task_id, rep, n, failure, ref, error, r.wall_s, r.cost, _now(), r.model_used,
                                      r.adapter_version)
                    store.append_attempt(att)
                    spent += att.cost.usd
                    last[(task_id, rep)] = att

        call(Verb.TEARDOWN, {}, b.build_timeout_s, "_teardown")   # failure logged in its stderr.log, not fatal
        store.seal(Seal(run_id, _now(), ()))
        shutil.rmtree(scratch, ignore_errors=True)
        return run_id


def reproduce(store: Store, catalog: Catalog, run_id: str, *, evaluator: str) -> RunPlan | Incompatibility:
    """Second-evaluator reproduction (acceptance #6): the original manifest's
    plan with purpose=REPRODUCTION. Fails with Incompatibility if the config or
    package hash no longer matches the catalog (the catalog changed under you)."""
    m = store.run(run_id).manifest
    if m.evaluator == evaluator:
        raise ValueError("a reproduction needs a different evaluator than the original run's")
    if m.plan.purpose is not Purpose.SCORED:
        raise ValueError("only SCORED runs can be reproduced")
    p = m.plan
    reasons = []
    if catalog.configs.get(p.config.config_id) is None or catalog.configs[p.config.config_id].identity_hash != p.config.identity_hash:
        reasons.append("configuration differs from the original run's")
    if catalog.packages.get(str(p.package)) is None or catalog.package(p.package).ref != p.package:
        reasons.append("package is missing or its content hash differs from the original run's")
    if reasons:
        inc = Incompatibility(p.config.config_id, p.package, p.mode, p.track, tuple(reasons))
        store.append_incompatibility(inc)
        return inc
    return plan(store, catalog, config_id=p.config.config_id, package=str(p.package), mode=p.mode, track=p.track,
                purpose=Purpose.REPRODUCTION, repetitions=p.repetitions)
