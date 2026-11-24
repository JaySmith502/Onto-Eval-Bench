"""FR-10/11/13: static report site and evidence export. Rendering only; every
number comes from compare.py.

Rendering rules that keep the hard rules visible:
- one comparison table per Cohort; tables are never merged, and a table's
  header always prints key.mode, key.track and the exact package ref
- ordering by score is allowed only inside a table; there is no cross-table
  or composite ranking anywhere in the output
- every score cell links to its task drill-down: manifest_id, attempts,
  decision(s), resolution, artifacts (subject to disclosure)
- status counts, n_tasks, n_groups and population.note print beside scores
- verdicts use bounded language: "on <package> under <mode>, A scored higher
  than B" / "inconclusive"; never "best platform"
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import statistics
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import compare, scoring
from .catalog import Catalog
from .model import (
    Cohort, ComparabilityKey, CorrectionKind, Disclosure, METHODOLOGY_VERSION, Purpose, Status,
)
from .store import Store, to_jsonable

_REDACTED = "[redacted]"


@dataclass(frozen=True)
class Blocker:
    what: str        # e.g. "run abc123 unsealed", "outcome r/t/2 disputed"
    fix: str         # e.g. "obs adjudicate r/t/2 ..."


class PublishBlocked(RuntimeError):
    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__("; ".join(b.what for b in blockers))
        self.blockers = blockers


def blockers(store: Store, catalog: Catalog) -> list[Blocker]:
    """Why publishing would violate acceptance criteria. Empty list = publishable.
    - any SCORED run in a cohort unsealed, or its executable scoring does not verify
    - any cohort outcome UNRESOLVED (unreviewed rubric task or dispute)
    - a cohort member without a qualifying REPRODUCTION run (acceptance #6): sealed, not void,
      same configuration identity and key, by an evaluator who ran none of the member's runs,
      no UNRESOLVED outcome, and executable scoring that verifies exactly
    - a VOID_RUN no second person has confirmed
    - nothing to publish at all
    """
    out: list[Blocker] = []
    corr = store.corrections()
    out += [Blocker(f"void of run {v.target} by {v.author} unconfirmed",
                    f"obs confirm-void {v.target} --author <someone other than {v.author}>")
            for v in corr if v.kind is CorrectionKind.VOID_RUN and not any(
                c.kind is CorrectionKind.CONFIRM_VOID and c.target == v.target and c.author != v.author for c in corr)]
    cs = compare.cohorts(store, catalog)
    if not cs:
        out.append(Blocker("no scored runs", "obs plan, obs run, obs score"))
    repros = [r for r in store.runs() if r.manifest.plan.purpose is Purpose.REPRODUCTION and r.void is None and r.seal]
    for key, cohort in cs.items():
        for c, runs in cohort.runs.items():
            for r in runs:
                if r.seal is None:
                    out.append(Blocker(f"run {r.run_id} unsealed", "obs run <plan file> (resumes)"))
                elif scoring.verify(store, catalog, r.run_id):
                    out.append(Blocker(f"executable scoring of run {r.run_id} does not reproduce", f"obs verify {r.run_id}"))
                out += [Blocker(f"outcome {r.run_id}/{t}/{rep} unresolved",
                                f"obs review {r.run_id}/{t}/{rep} --value V --rationale ... --adjudicate")
                        for (t, rep), o in sorted(r.outcomes.items()) if o.status is Status.UNRESOLVED]
            ident, evaluators = runs[0].manifest.plan.config.identity_hash, {r.manifest.evaluator for r in runs}
            if not any(x.manifest.plan.key == key and x.manifest.plan.config.identity_hash == ident
                       and x.manifest.evaluator not in evaluators
                       and all(o.status is not Status.UNRESOLVED for o in x.outcomes.values())
                       and not scoring.verify(store, catalog, x.run_id) for x in repros):
                out.append(Blocker(f"no qualifying reproduction run of {c} in {key.package} {key.mode} {key.track}",
                                   f"obs reproduce <run_id> --evaluator <not {', '.join(sorted(evaluators))}>, "
                                   "then obs run and obs score as that evaluator"))
    return out


# ------------------------------------------------------------------ rendering

_e = html.escape
_CSS = "body{font:14px sans-serif;margin:2em auto;max-width:70em}table{border-collapse:collapse;margin:1em 0}" \
       "td,th{border:1px solid #bbb;padding:.2em .5em;text-align:left}.draft{background:#fc0;padding:.3em}"


def _table(head: list[str], rows: list[list[str]]) -> str:
    """Cells are HTML; callers escape."""
    return ("<table><tr>" + "".join(f"<th>{_e(h)}</th>" for h in head) + "</tr>"
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>")


def _page(title: str, body: str, draft: bool) -> str:
    banner = '<p class="draft">DRAFT: not published</p>' if draft else ""
    return (f"<!doctype html><meta charset=utf-8><title>{_e(title)}</title><style>{_CSS}</style>"
            f"{banner}<h1>{_e(title)}</h1>{body}")


def _fmt(i) -> str:
    return f"{i.estimate:.3f} [{i.low:.3f}, {i.high:.3f}]"


def _statement(c: compare.Comparison, p: compare.PairedComparison) -> str:
    where = f"on {c.key.package} under {c.key.mode}"
    return {compare.Call.A_BETTER: f"{where}, {p.a} scored higher than {p.b}",
            compare.Call.B_BETTER: f"{where}, {p.b} scored higher than {p.a}",
            compare.Call.EQUIVALENT: f"{where}, {p.a} and {p.b} were equivalent within the pre-registered margin",
            compare.Call.INCONCLUSIVE: f"{where}, inconclusive for {p.a} vs {p.b}"}[p.call]


def _title(key: ComparabilityKey) -> str:
    model = f", imposed model {key.model[0]} {key.model[1]}" if key.model else ""
    return f"{key.package} / {key.mode} / {key.track}{model}"


def _drill(store: Store, cohort: Cohort, config_id: str, pop: compare.Population) -> str:
    tasks = {t.task_id: t for t in cohort.package.tasks}
    rows = []
    for r in cohort.runs[config_id]:
        for (t, rep), o in sorted(r.outcomes.items()):
            if t not in pop.task_ids:
                continue
            atts = [a for a in r.attempts if (a.task_id, a.repetition) == (t, rep)]
            final = r.final_attempts.get((t, rep))
            restricted = tasks[t].disclosure is Disclosure.RESTRICTED
            text = _REDACTED if restricted else "; ".join(
                f"#{a.attempt_no} {a.failure.name if a.failure else 'ok'} {a.error}".strip() for a in atts)
            outs = "" if restricted or not final or not final.output_ref else (store.root / final.output_ref).read_text("utf-8")
            decs = [d for d in r.decisions if d.outcome_id == f"{r.run_id}/{t}/{rep}"]
            rows.append([_e(r.run_id), _e(t), str(rep), _e(o.status.label), f"{o.credit:.3f}", _e(o.manifest_id),
                         _e(", ".join(f"{d.kind.name}:{d.decision_id}" for d in decs) or "-"), _e(text), _e(outs)])
    return _table(["run", "task", "rep", "status", "credit", "manifest", "decisions", "attempts", "output"], rows)


def _render(store: Store, catalog: Catalog, out: Path, draft: bool, pub_note: str = "") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "drill").mkdir(exist_ok=True)
    cs = compare.cohorts(store, catalog)
    body = [f"<p>Methodology version {_e(METHODOLOGY_VERSION)}; taxonomy version {_e(catalog.taxonomy_version)}. "
            "Cohorts are never merged and nothing here is a composite ranking.</p>", pub_note]
    for n, (key, cohort) in enumerate(cs.items()):
        c = compare.compare(cohort)
        an = cohort.package.analysis
        rows = []
        for s in c.summaries:
            link = f'<a href="drill/{n}-{_e(s.config_id)}.html">{s.score.estimate:.3f}</a> [{s.score.low:.3f}, {s.score.high:.3f}]'
            (out / "drill" / f"{n}-{s.config_id}.html").write_text(
                _page(f"{s.config_id}: {_title(key)}", _drill(store, cohort, s.config_id, c.population), draft), "utf-8")
            cost = f"${s.usd_measured:.2f} measured" + (f" + ${s.usd_estimated:.2f} estimated" if s.usd_estimated else "")
            rows.append([_e(s.config_id), link, ", ".join(f"{k.name}={v}" for k, v in sorted(s.status_counts.items(), key=lambda kv: kv[0].name)),
                         f"{s.critical_errors} critical of {s.n_scored} scored ({sum(s.status_counts.values()) - s.n_scored} not scored)",
                         str(s.n_runs), "-" if s.run_to_run_sd is None else f"{s.run_to_run_sd:.3f}",
                         f"{s.wall_s:.1f}s", cost, f"assisted ({s.human_minutes:g} min)" if s.human_minutes else "no"])
        prow = [[_e(p.a), _e(p.b), _fmt(p.diff), _fmt(p.diff_task_level), _e(p.call.value), _e(_statement(c, p))] for p in c.pairs]
        body.append(
            f"<h2>{_e(_title(key))}</h2><p>{_e(c.population.note)}; {c.population.n_groups} groups "
            f"(min {an.min_groups}); margin {an.min_meaningful_diff}; {an.bootstrap_resamples} cluster-bootstrap resamples, "
            f"multiplicity {_e(an.multiplicity)}. Task-level interval is a sensitivity figure only. "
            f"Unresolved outcomes: {len(c.unresolved)}.</p>"
            + _table(["config", "mean task credit [95% CI]", "status counts", "critical errors", "runs",
                      "run-to-run sd", "wall", "cost", "assisted"], rows)
            + _table(["A", "B", "A minus B [95% CI, group bootstrap]", "task-level (sensitivity)", "call", "statement"], prow))
    cov = compare.coverage(catalog, store)
    body.append("<h2>Coverage</h2><p>Evidence, not claims, decides EVALUATED. Hover for why.</p>" + _table(
        ["config"] + list(cov.tags),
        [[_e(c)] + [f'<span title="{_e(cov.evidence[(c, t)])}">{_e(cov.cells[(c, t)].value)}</span>' for t in cov.tags]
         for c in cov.config_ids]))
    body.append("<h2>Disclosures</h2><ul>" + "".join(
        f"<li>{_e(c.name)}: {_e('; '.join(c.disclosures) or 'none')} (claims: {_e(c.claim_source)})</li>"
        for c in catalog.candidates.values()) + "".join(
        f"<li>{_e(str(p.ref))}: {_e('; '.join(p.limitations) or 'no stated limitations')}</li>" for p in catalog.packages.values()) + "</ul>")
    body.append("<h2>Incompatibilities</h2>" + _table(["config", "package", "mode", "track", "reasons"], [
        [_e(i.config_id), _e(str(i.package)), _e(i.mode), _e(i.track), _e("; ".join(i.reasons))] for i in store.incompatibilities()]))
    body.append("<h2>Corrections</h2>" + _table(["at", "author", "kind", "target", "reason class", "reason"], [
        [_e(c.at.isoformat()), _e(c.author), _e(c.kind), _e(c.target), _e(c.reason_class or "-"), _e(c.reason)]
        for c in store.corrections()]))
    runs = list(store.runs())
    confirmed = {c.target: c.author for c in store.corrections() if c.kind is CorrectionKind.CONFIRM_VOID}
    body.append("<h2>Voided runs</h2><p>Excluded from every cohort; shown so a void cannot hide a score.</p>" + _table(
        ["run", "config", "evaluator", "reason class", "reason", "voided by", "confirmed by", "mean credit", "status counts"],
        [[_e(r.run_id), _e(r.manifest.plan.config.config_id), _e(r.manifest.evaluator), _e(r.void.reason_class),
          _e(r.void.reason), _e(r.void.author), _e(confirmed.get(r.run_id, "unconfirmed")),
          f"{statistics.fmean(o.credit for o in r.outcomes.values()):.3f}",
          _e(", ".join(f"{s.name}={n}" for s, n in sorted(Counter(o.status for o in r.outcomes.values()).items(),
                                                            key=lambda kv: kv[0].name)))]
         for r in runs if r.void is not None and r.manifest.plan.purpose is Purpose.SCORED]))
    rep_rows = []
    for r in runs:
        p = r.manifest.plan
        if p.purpose is not Purpose.REPRODUCTION:
            continue
        for o in runs:
            q = o.manifest.plan
            if q.purpose is Purpose.SCORED and q.key == p.key and q.config.config_id == p.config.config_id:
                same = sum((x.status, x.credit) == (o.outcomes[k].status, o.outcomes[k].credit)
                           for k, x in r.outcomes.items() if k in o.outcomes)
                rep_rows.append([_e(r.run_id), _e(r.manifest.evaluator), _e(o.run_id), _e(o.manifest.evaluator),
                                 f"{same} of {len(r.outcomes)}"])
    body.append("<h2>Reproduction agreement</h2>" + _table(
        ["reproduction run", "evaluator", "original run", "evaluator", "identical (status, credit) cells"], rep_rows))
    (out / "index.html").write_text(_page("Ontology Evaluation Observatory", "".join(body), draft), "utf-8")
    export(store, catalog, out / "export")


def build(store: Store, catalog: Catalog, out: Path) -> Path:
    """Render a DRAFT site to `out` (overwritable; watermarked "draft").
    Pages: index (cohort tables with pairwise comparisons, coverage matrix, disclosures,
    incompatibilities, corrections, reproduction agreement) and one task drill-down per
    (cohort, config). Also writes `out/export/` (see export)."""
    _render(store, catalog, out, draft=True)
    return out


def publish(store: Store, catalog: Catalog, *, editor: str) -> Path:
    """Immutable snapshot at store/publications/<pub_id>/ where pub_id = date + content hash of
    the rendered site without its history list. Raises PublishBlocked if blockers() is non-empty.
    Idempotent: the same evidence returns the existing publication. Never overwrites; a new
    publication is staged and renamed into place whole. Prior publications stay and the index
    links them all (historical results, FR-11). Corrections after publication are Correction
    records and appear in the next publication's history."""
    if bl := blockers(store, catalog):
        raise PublishBlocked(bl)
    base = store.root / "publications"
    with tempfile.TemporaryDirectory() as tmp:
        _render(store, catalog, Path(tmp), draft=False)
        h = hashlib.sha256()
        for f in sorted(x for x in Path(tmp).rglob("*") if x.is_file()):
            h.update(f.relative_to(tmp).as_posix().encode() + b"\0" + f.read_bytes())
    digest = h.hexdigest()[:12]
    prior = sorted(p.name for p in base.iterdir() if not p.name.startswith(".")) if base.exists() else []
    if same := [p for p in prior if p.endswith(f"-{digest}")]:
        return base / same[0]
    when = datetime.now(timezone.utc)
    pub_id = f"{when.date().isoformat()}-{digest}"
    stage = base / f".{pub_id}.staging"
    shutil.rmtree(stage, ignore_errors=True)
    note = "<h2>Publications</h2><ul>" + "".join(f'<li><a href="../{_e(p)}/index.html">{_e(p)}</a></li>' for p in prior) + "</ul>"
    _render(store, catalog, stage, draft=False, pub_note=note)
    (stage / "publication.json").write_text(json.dumps({"pub_id": pub_id, "editor": editor, "published_at": when.isoformat()}))
    os.rename(stage, base / pub_id)
    return base / pub_id


def export(store: Store, catalog: Catalog, out: Path) -> Path:
    """FR-13 machine-readable bundle: manifests, attempts, decisions, cells,
    summaries, paired comparisons, coverage, as JSON. RESTRICTED tasks keep
    ids, groups, tags, statuses and scores; their inputs, references,
    outputs, and free-text notes are redacted. The bundle is sufficient to
    recompute every published number with compare.py (a second evaluator's check)."""
    restricted = {(str(p.ref), t.task_id) for p in catalog.packages.values() for t in p.tasks
                  if t.disclosure is Disclosure.RESTRICTED}
    runs, content = [], {}
    for r in store.runs():
        pk = str(r.manifest.plan.package)
        red = lambda task: (pk, task) in restricted
        attempts = [replace(a, error=_REDACTED, output_ref=None) if red(a.task_id) else a for a in r.attempts]
        decisions = [replace(d, rationale=_REDACTED) if red(d.outcome_id.split("/", 2)[1]) else d for d in r.decisions]
        cells = [{"outcome_id": f"{r.run_id}/{t}/{rep}", "status": o.status.name, "credit": o.credit,
                  "manifest_id": o.manifest_id, "decision_id": o.decision_id} for (t, rep), o in sorted(r.outcomes.items())]
        runs.append(to_jsonable({"manifest": r.manifest, "build": r.build, "attempts": attempts, "interventions": r.interventions,
                                 "seal": r.seal, "decisions": decisions, "void": r.void, "cells": cells}))
        pdir = catalog.package_dir(r.manifest.plan.package)
        for a in r.attempts:
            if a.output_ref and not red(a.task_id):
                content.setdefault(pk, {}).setdefault(a.task_id, {"outputs": {}})["outputs"][a.outcome_id + f"#{a.attempt_no}"] = \
                    json.loads((store.root / a.output_ref).read_text("utf-8"))
        for t in catalog.package(r.manifest.plan.package).tasks:
            if not red(t.task_id) and t.task_id in r.manifest.plan.task_ids:
                slot = content.setdefault(pk, {}).setdefault(t.task_id, {"outputs": {}})
                slot["input"] = json.loads((pdir / t.input_ref).read_text("utf-8"))
                slot["reference"] = json.loads((pdir / t.reference_ref).read_text("utf-8")) if t.reference_ref else None
    comparisons = [compare.compare(c) for c in compare.cohorts(store, catalog).values()]
    tasks = {str(p.ref): [to_jsonable({"task_id": t.task_id, "group": t.group, "tracks": t.tracks, "tags": t.tags,
                                       "split": t.split, "disclosure": t.disclosure}) for t in p.tasks]
             for p in catalog.packages.values()}
    bundle = {"methodology_version": METHODOLOGY_VERSION, "taxonomy_version": catalog.taxonomy_version,
              "packages": {str(p.ref): to_jsonable({"ref": p.ref, "analysis": p.analysis, "budgets": p.budgets,
                                                    "controlled_model": p.controlled_model}) for p in catalog.packages.values()},
              "tasks": tasks, "task_content": content, "runs": runs, "comparisons": to_jsonable(comparisons),
              "coverage": to_jsonable(compare.coverage(catalog, store)), "incompatibilities": to_jsonable(store.incompatibilities()),
              "corrections": to_jsonable(store.corrections())}
    out.mkdir(parents=True, exist_ok=True)
    (out / "bundle.json").write_text(json.dumps(bundle, sort_keys=True, indent=1), "utf-8")
    return out
