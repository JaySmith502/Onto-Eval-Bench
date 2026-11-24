"""Every published number comes from here. Pure functions over a Cohort
(FR-09, FR-10, FR-12). No function takes two cohorts and none produces a
composite score, so the forbidden rankings have no code path."""

from __future__ import annotations

import enum
import random
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations

from .catalog import Catalog
from .model import METHODOLOGY_VERSION, ClaimStatus, Cohort, ComparabilityKey, Purpose, Split, Status
from .store import Store


def cohorts(store: Store, catalog: Catalog) -> dict[ComparabilityKey, Cohort]:
    """Partition of every non-voided SCORED run by plan.key, then config_id.
    The only way the CLI and the report obtain a Cohort. Because it is a
    partition, mixing modes or package versions is not expressible."""
    grouped: dict[ComparabilityKey, dict[str, list]] = {}
    for r in store.runs():
        p = r.manifest.plan
        if p.purpose is Purpose.SCORED and r.void is None:
            grouped.setdefault(p.key, {}).setdefault(p.config.config_id, []).append(r)
    return {k: Cohort(k, catalog.package(k.package), {c: tuple(rs) for c, rs in sorted(g.items())})
            for k, g in sorted(grouped.items(), key=lambda kv: (str(kv[0].package), kv[0].mode, kv[0].track, kv[0].model or ()))}


@dataclass(frozen=True)
class Population:
    """The declared common task set of one comparison."""
    task_ids: tuple[str, ...]
    n_groups: int
    config_ids: tuple[str, ...]
    note: str            # e.g. "72 of 100 eligible tasks (tag task:querying)"


def population(cohort: Cohort, configs: Iterable[str] | None = None,
               tags: frozenset[str] = frozenset()) -> Population:
    """Common-task rule. Tasks = package.eligible(key.track, HELDOUT), filtered
    by `tags` identically for every member. No candidate outcome can remove a
    task: failures, timeouts, unsupported, missing and unresolved all stay at
    zero credit. Selecting different configs recomputes the population; it
    never changes the task set, only the members. Unknown config ids raise.
    A task passes the tag filter when it carries every tag in `tags`."""
    members = tuple(sorted(cohort.runs if configs is None else set(configs)))
    if unknown := [c for c in members if c not in cohort.runs]:
        raise ValueError(f"not in this cohort: {unknown}")
    eligible = cohort.package.eligible(cohort.key.track, Split.HELDOUT)
    chosen = tuple(t for t in eligible if tags <= t.tags)
    if not chosen:
        raise ValueError(f"no eligible tasks carry all of {sorted(tags)}")
    note = f"{len(chosen)} of {len(eligible)} eligible tasks" + (f" (tags {', '.join(sorted(tags))})" if tags else "")
    return Population(tuple(t.task_id for t in chosen), len({t.group for t in chosen}), members, note)


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float


class Call(enum.StrEnum):
    A_BETTER = "a_better"              # diff CI above 0 and estimate >= mmd
    B_BETTER = "b_better"
    EQUIVALENT = "equivalent"          # CI inside (-mmd, +mmd)
    INCONCLUSIVE = "inconclusive"      # anything else, incl. too few groups


@dataclass(frozen=True)
class ConfigSummary:
    config_id: str
    score: Interval                     # mean task credit; task credit = mean over all runs x reps
    critical_error_rate: float
    status_counts: Mapping[Status, int]  # always printed beside the score
    n_runs: int
    run_to_run_sd: float | None
    wall_s: float
    usd_measured: float
    usd_estimated: float
    human_minutes: float                 # > 0 => "assisted" label
    trace: Mapping[str, tuple[tuple[str, str | None], ...]]   # task -> ((manifest_id, decision_id), ...)


@dataclass(frozen=True)
class PairedComparison:
    a: str
    b: str
    diff: Interval                      # cluster bootstrap over groups
    diff_task_level: Interval           # sensitivity figure, never used for the call
    call: Call


@dataclass(frozen=True)
class Comparison:
    """A finished common-task comparison. No rank field: order is expressed
    only through pairwise calls."""
    key: ComparabilityKey
    population: Population
    summaries: tuple[ConfigSummary, ...]   # ordered by config_id, not score
    pairs: tuple[PairedComparison, ...]
    unresolved: tuple[str, ...]            # outcome ids; non-empty blocks publication
    methodology_version: str


def compare(cohort: Cohort, configs: Iterable[str] | None = None,
            tags: frozenset[str] = frozenset()) -> Comparison:
    """Summaries, paired differences and uncertainty over the population.

    Seed, resamples, margin, min_groups and multiplicity come from
    cohort.package.analysis (pre-registered), never from the caller.
    """
    pop = population(cohort, configs, tags)
    plan = cohort.package.analysis
    group_of = {t.task_id: t.group for t in cohort.package.tasks}
    groups: dict[str, list[int]] = {}
    for i, t in enumerate(pop.task_ids):
        groups.setdefault(group_of[t], []).append(i)
    clusters = [groups[g] for g in sorted(groups)]
    credit: dict[str, list[float]] = {}
    for c in pop.config_ids:
        cells = {t: [] for t in pop.task_ids}
        for r in cohort.runs[c]:
            for (t, _), o in r.outcomes.items():
                if t in cells:
                    cells[t].append(o.credit)
        credit[c] = [statistics.fmean(cells[t]) for t in pop.task_ids]

    def cluster_boot(v: list[float], seed: str) -> list[float]:
        rng, sums = random.Random(seed), [(sum(v[i] for i in g), len(g)) for g in clusters]
        out = []
        for _ in range(plan.bootstrap_resamples):
            pick = rng.choices(sums, k=len(sums))
            out.append(sum(s for s, _ in pick) / sum(n for _, n in pick))
        return out

    def task_boot(v: list[float], seed: str) -> list[float]:
        rng = random.Random(seed)
        return [statistics.fmean(rng.choices(v, k=len(v))) for _ in range(plan.bootstrap_resamples)]

    def interval(v: list[float], draws: list[float]) -> Interval:
        xs = sorted(draws)
        return Interval(statistics.fmean(v), xs[int(0.025 * len(xs))], xs[min(len(xs) - 1, int(0.975 * len(xs)))])

    in_pop = set(pop.task_ids)
    summaries = []
    for c in pop.config_ids:
        outs = [(r, o) for r in cohort.runs[c] for (t, _), o in r.outcomes.items() if t in in_pop]
        scored = [o for _, o in outs if o.verdict is not None]
        per_run = [statistics.fmean(o.credit for rr, o in outs if rr is r) for r in cohort.runs[c]]
        atts = [a for r in cohort.runs[c] for a in r.attempts if a.task_id in in_pop]
        builds = [r.build.cost for r in cohort.runs[c] if r.build]
        costs = [a.cost for a in atts] + builds
        trace: dict[str, list[tuple[str, str | None]]] = {}
        for _, o in outs:
            trace.setdefault(o.task_id, []).append((o.manifest_id, o.decision_id))
        summaries.append(ConfigSummary(
            c, interval(credit[c], cluster_boot(credit[c], f"{plan.seed}:{c}")),
            sum(o.verdict.critical_error for o in scored) / len(scored) if scored else 0.0,
            dict(Counter(o.status for _, o in outs)), len(cohort.runs[c]),
            statistics.stdev(per_run) if len(per_run) > 1 else None,
            sum(a.wall_s for a in atts) + sum(r.build.wall_s for r in cohort.runs[c] if r.build),
            sum(x.usd for x in costs if x.measured), sum(x.usd for x in costs if not x.measured),
            sum(iv.minutes for r in cohort.runs[c] for iv in r.interventions),
            {t: tuple(v) for t, v in trace.items()}))

    raw = []
    for a, b in combinations(pop.config_ids, 2):
        d = [x - y for x, y in zip(credit[a], credit[b])]
        draws = cluster_boot(d, f"{plan.seed}:{a}:{b}")
        est, iv = statistics.fmean(d), interval(d, draws)
        p = min(1.0, 2 * min(sum(x <= 0 for x in draws), sum(x >= 0 for x in draws)) / len(draws))
        mmd = plan.min_meaningful_diff
        if pop.n_groups < plan.min_groups:
            call = Call.INCONCLUSIVE
        elif iv.low > -mmd and iv.high < mmd:
            call = Call.EQUIVALENT
        elif iv.low > 0 and est >= mmd:
            call = Call.A_BETTER
        elif iv.high < 0 and est <= -mmd:
            call = Call.B_BETTER
        else:
            call = Call.INCONCLUSIVE
        raw.append((a, b, iv, interval(d, task_boot(d, f"{plan.seed}:{a}:{b}:task")), call, p))
    stands = set(range(len(raw)))
    if plan.multiplicity == "holm":   # step-down: a directional call stands only while it is Holm-significant
        stands = set()
        for k, i in enumerate(sorted(range(len(raw)), key=lambda i: raw[i][5])):
            if raw[i][5] > 0.05 / (len(raw) - k):
                break
            stands.add(i)
    pairs = tuple(PairedComparison(a, b, iv, tl, call if call not in (Call.A_BETTER, Call.B_BETTER) or i in stands
                                   else Call.INCONCLUSIVE)
                  for i, (a, b, iv, tl, call, _) in enumerate(raw))
    unresolved = tuple(sorted(f"{r.run_id}/{t}/{rep}" for c in pop.config_ids for r in cohort.runs[c]
                              for (t, rep), o in r.outcomes.items() if t in in_pop and o.status is Status.UNRESOLVED))
    return Comparison(cohort.key, pop, tuple(summaries), pairs, unresolved, METHODOLOGY_VERSION)


class CoverageState(enum.StrEnum):
    EVALUATED = "evaluated"
    SUPPORTED_NOT_EVALUATED = "supported_not_evaluated"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CoverageMatrix:
    taxonomy_version: str
    config_ids: tuple[str, ...]
    tags: tuple[str, ...]                                 # every taxonomy tag, tested or not
    cells: Mapping[tuple[str, str], CoverageState]
    evidence: Mapping[tuple[str, str], str]               # why: "12 scored outcomes" / "claim: <src>" / "incompatible: <reason>"


def coverage(catalog: Catalog, store: Store) -> CoverageMatrix:
    """Per (config, taxonomy tag), first match wins:
    1. a non-voided SCORED run outcome on a tagged task, other than UNSUPPORTED -> EVALUATED
    2. tagged outcomes all UNSUPPORTED, a claim UNSUPPORTED, or an Incompatibility -> UNSUPPORTED
    3. claim SUPPORTED -> SUPPORTED_NOT_EVALUATED
    4. claim NOT_APPLICABLE -> NOT_APPLICABLE
    5. otherwise -> UNKNOWN
    Measured evidence always beats claims. A config never run is never a measured failure,
    and MISSING outcomes (planned, no attempt yet) are not evidence either."""
    tags_of = {str(p.ref): {t.task_id: t.tags for t in p.tasks} for p in catalog.packages.values()}
    seen: dict[tuple[str, str], Counter] = {}
    for r in store.runs():
        p = r.manifest.plan
        if p.purpose is not Purpose.SCORED or r.void is not None:
            continue
        for (t, _), o in r.outcomes.items():
            for tag in tags_of[str(p.package)][t]:
                seen.setdefault((p.config.config_id, tag), Counter())[o.status] += 1
    incompat = store.incompatibilities()
    cells, evidence = {}, {}
    for c, cfg in sorted(catalog.configs.items()):
        claims = dict(catalog.candidates[cfg.method_id].claims)
        src = catalog.candidates[cfg.method_id].claim_source
        for tag in catalog.taxonomy:
            n = seen.get((c, tag), Counter())
            measured = sum(v for k, v in n.items() if k not in (Status.UNSUPPORTED, Status.MISSING))
            inc = next((i for i in incompat if i.config_id == c and any(tag in t for t in tags_of[str(i.package)].values())), None)
            if measured:
                state, why = CoverageState.EVALUATED, f"{measured} outcomes in scored runs"
            elif n[Status.UNSUPPORTED]:
                state, why = CoverageState.UNSUPPORTED, f"{n[Status.UNSUPPORTED]} outcomes, all unsupported"
            elif claims.get(tag) is ClaimStatus.UNSUPPORTED:
                state, why = CoverageState.UNSUPPORTED, f"claim: {src}"
            elif inc:
                state, why = CoverageState.UNSUPPORTED, "incompatible: " + "; ".join(inc.reasons)
            elif claims.get(tag) is ClaimStatus.SUPPORTED:
                state, why = CoverageState.SUPPORTED_NOT_EVALUATED, f"claim: {src}"
            elif claims.get(tag) is ClaimStatus.NOT_APPLICABLE:
                state, why = CoverageState.NOT_APPLICABLE, f"claim: {src}"
            else:
                state, why = CoverageState.UNKNOWN, "no claim and no evidence"
            cells[(c, tag)], evidence[(c, tag)] = state, why
    return CoverageMatrix(catalog.taxonomy_version, tuple(sorted(catalog.configs)), catalog.taxonomy, cells, evidence)
