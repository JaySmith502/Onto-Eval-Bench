# Pilot: sec-fin@0.3.0 (2026-11-24)

## Why this package exists

The sec-fin@0.2.0 pilot left too little headroom. Both baselines scored about 0.92 and six of ten groups sat at or near 1.0 (see `sec-fin-0.2.0.md`). 0.3.0 adds dimensional (segment) facts, which carry the axis and member structure an ontology encodes, and adds more multi-concept composition tasks.

The data has three new parts:
- `dnum`: facts that carry dimensions.
- `dim`: one row per axis/member pair.
- `dimensions.json`: axis and member labels, given only to the `definitions` access level.

The package still has 100 tasks, now in 11 groups. Three groups are new: `segment_lookup`, `segment_aggregate` and `segment_composition`.

## Review before the pilot

A reviewer on a different model checked all 100 tasks against the database before any model ran on them. It found 89 pass, 10 minor, 1 ambiguous and 0 wrong. Every gold query reproduced its stored reference. All items on the reviewer's fix list were applied:

- `sa_southern_coregistrants_ni` had two defensible sums. It now asks for income attributable to each subsidiary, after noncontrolling interests.
- `sa_amazon_segment_opinc` and `sa_msft_segment_opinc` equalled consolidated operating income, so a query that never touched `dnum` could score. Each now asks for two named segments.
- Scope, unit and rounding wording was fixed. One tolerance was relaxed to relative 0.001, because the filer's own US and non-US figures round to a sum 1M off.
- `schema.sql` now documents three things:
  - Some filers add a second `ConsolidationItems=OperatingSegments` pair to segment facts, so `ndims` varies by filer.
  - The `LegalEntity` axis holds more than co-registrants.
  - `dnum` carries other currencies, and `ddate` is rounded to month end.

Two known limitations remain. They are stated in `package.toml` and in the rationale:
- **Concentration.** Four groups (32 tasks) draw from six 10-K filers only.
- **Abstention by query shape.** `COALESCE(lookup, 'NOT_IN_DATABASE')` scores on every unanswerable task whenever the lookup misses for any reason.

Tasks were never selected or dropped by model score.

## Pilot result

Each config ran 89 held-out tasks with 3 repetitions:
- **Haiku 5.5:** controlled mode, the package's controlled model.
- **Sonnet 5.5:** native mode, as a second-model check on headroom.

The adapter is `llm-sql-3`, the containers run in Docker, and the pilot cost $9.62 in all.

| Config | Score | 95% CI | Cost | Failures |
|---|---|---|---|---|
| baseline-schema-haiku | 0.801 | [0.687, 0.919] | $0.28 | 6 runtime, 1 invalid output |
| baseline-context-haiku | 0.798 | [0.688, 0.903] | $0.40 | 6 runtime |
| baseline-schema-sonnet | 0.790 | [0.670, 0.903] | $3.94 | 6 runtime |
| baseline-context-sonnet | 0.674 | [0.567, 0.779] | $5.00 | 6 runtime, 1 invalid output, 50 budget exhausted |

Haiku schema vs context: a difference of -0.004, 95% CI [-0.061, +0.053], inconclusive.

Sonnet context vs schema: a difference of -0.116, 95% CI [-0.165, -0.063]. This difference is mostly caused by the budget, explained below; it is not evidence that the context hurts.

Scores by group follow. Failures count as 0. Sonnet context\* excludes the 50 budget-exhausted attempts.

| Group | Haiku schema | Haiku context | Sonnet schema | Sonnet context | Sonnet context\* |
|---|---|---|---|---|---|
| lookup | 0.87 | 0.80 | 0.80 | 0.93 | 1.00 |
| aggregate_sector | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| unanswerable | 1.00 | 0.96 | 1.00 | 0.96 | 1.00 |
| period_compare | 1.00 | 0.93 | 1.00 | 0.93 | 1.00 |
| ratio | 1.00 | 0.95 | 0.86 | 0.76 | 0.81 |
| aggregate_rank | 1.00 | 1.00 | 0.86 | 0.95 | 1.00 |
| disambiguation | 0.81 | 0.81 | 0.62 | 0.62 | 0.73 |
| concept_composition | 0.97 | 0.82 | 0.91 | 0.73 | 0.86 |
| segment_lookup | 0.73 | 0.88 | 0.94 | 0.88 | 1.00 |
| segment_aggregate | 0.44 | 0.42 | 0.53 | 0.53 | 0.68 |
| segment_composition | 0.70 | 0.70 | 0.58 | 0.42 | 0.50 |

The sentinel `NOT_IN_DATABASE` was also returned on answerable tasks, where it scores 0:

| Config | Answerable attempts that returned the sentinel |
|---|---|
| Haiku schema | 22 / 239 (9%) |
| Haiku context | 19 / 238 (8%) |
| Sonnet schema | 36 / 238 (15%) |
| Sonnet context | 19 / 189 (10%) |

Two tasks drew the sentinel from all four configs: `cc_msft_sm_ga` and `sa_cat_intersegment`. Inspection shows model errors, not task defects. In both, a model's query missed a tag or member that is in the data:
- `CostOfRevenue` where Microsoft files `CostOfGoodsAndServicesSold`.
- `IntersegmentEliminations` where the member is `IntersegmentElimination`.

The model then wrapped the miss in `COALESCE(..., 'NOT_IN_DATABASE')`.

## Run incidents

- **First Sonnet launch.** Every attempt failed in about 1 s with `KeyError: 'model'`. In native mode the runner imposes no model, and the adapter assumed it would. The fix (commit `adef7e9`) makes the adapter read `model_id` and `model_version` from the config's `[settings]`, and adds a test. The broken runs were stopped and kept out of the comparison store.
- **Budget.** Context-Sonnet reached the package's $5 native cap after 210 of 267 attempts, at about $0.024 and 6.7k input tokens per attempt. The cap was not raised, because that would change the package hash the Haiku runs are scored under.
- **Transient network failures.** Each config had 6 runtime failures (about 2%) from dropped API connections ("Remote end closed connection", SSL EOF). The rate is the same across configs. The adapter does not retry transport errors.

## Reading

0.3.0 has the headroom 0.2.0 lacked:
- Both Haiku baselines fell from about 0.92 to about 0.80.
- The segment groups score 0.42 to 0.94.
- `segment_aggregate` (0.42 to 0.68) and `segment_composition` (0.42 to 0.70) are the hardest groups for every config.

Sonnet does not close the gap. On equal footing (schema only, full budget) it scores 0.790 against Haiku's 0.801. Its disambiguation and ratio scores are lower, and it returns the abstention sentinel more often on answerable tasks (15% against 9%). So the headroom is not an artifact of Haiku being weak. The cost is that the sentinel acts as an escape hatch: a model that cannot find a fact abstains instead of guessing, and is scored 0.

Definitions and labels still do not help a plain retrieval baseline (Haiku: -0.004, inconclusive). This is the gap an ontology method is meant to fill. Mapping a question to the right axis, member and concept is where every baseline loses points.

`aggregate_sector` is saturated at 1.00 for all four configs. `unanswerable` is at 0.96 to 1.00, partly because of the query-shape limitation above.

## Decision

Use 0.3.0 as the first package for ranking ontology methods. Before the next package version:
- Retry transport errors in the adapter (one retry with backoff) so network drops stop counting as method failures.
- Give the native budget room for the context tier with a stronger model, or report native runs only on completed attempts.
- Score abstention separately from accuracy, so that unanswerable-group credit earned by query shape is visible.
- Retire or harden `aggregate_sector`, and spread the four six-filer groups across more filers.
