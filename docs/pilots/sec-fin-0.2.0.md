# Pilot: sec-fin@0.2.0 (2026-11-24)

## Why this package exists

sec-fin@0.1.0 was a 12-task vertical slice. In a live controlled run with `claude-haiku-5-5` (9 held-out tasks, 3 repetitions), the schema-only baseline scored 0.815 and the schema+definitions baseline scored 1.000. A method cannot score above 1.000, so 0.1.0 could not rank any ontology method against the context baseline. 0.2.0 grows the package to 100 tasks in ten scenario groups to make it harder and broader.

## Review before the pilot

An independent reviewer on a different model checked all 100 tasks against the database before any model ran on them. It reported 81 pass, 11 minor wording, 7 ambiguous and 1 wrong reference, plus systemic issues. All were fixed (PR #2 commit 5357bc0):

- `schema.sql` named a unit, `USD/shares`, that does not exist in the data. It also described `prevrpt` as an amendment flag, but `prevrpt` is 0 on every row.
- `definitions.json` carried filer custom labels, such as a filer-specific label on `ProfitLoss`. It now uses standard US-GAAP labels only.
- Abstention was credited by query shape: a wrong tag guess that returned NULL scored as a correct "not reported". Abstention is now an explicit sentinel, `'NOT_IN_DATABASE'`.
- A co-registrant filing that copied its parent's facts was counted twice. Such filings are now dropped by a mechanical rule.
- Several "unanswerable" questions named companies that filed in the real world but were cut by the subset. They were replaced by facts that filers in the database genuinely do not report, and every question is now scoped to the database.

Tasks were never selected or dropped by model score.

## Pilot result

The run was in controlled mode with `claude-haiku-5-5`, 90 held-out tasks and 3 repetitions per config, 540 attempts in all. It cost $0.41.

| Config | Score | 95% CI |
|---|---|---|
| baseline-schema-haiku | 0.922 | [0.852, 0.978] |
| baseline-context-haiku | 0.919 | [0.815, 0.989] |

The paired difference was -0.004, with a 95% CI of [-0.041, +0.026], so the two configs are equivalent.

| Group | schema | context |
|---|---|---|
| lookup_instant, lookup_duration, aggregate_sector, unanswerable | 1.00 | 1.00 |
| period_compare | 0.96 | 1.00 |
| ratio | 0.96 | 0.96 |
| concept_mapping | 0.93 | 1.00 |
| aggregate_rank | 0.89 | 0.89 |
| disambiguation | 0.81 | 0.81 |
| concept_composition | 0.67 | 0.52 |

## Reading

The package is valid but leaves too little room above the baselines to rank methods. The baselines score about 0.92, so a method has at most about 8 points to gain. Six groups sit at or near ceiling.

Definitions stopped helping once the ambiguous wording was fixed. The likely reason is that US-GAAP element names are already descriptive English, so the plain lines can be found from the name alone. That is an inference; it has not been tested. Composing several concepts is the weakest group for both baselines.

## Decision

Build 0.3.0 with dimensional (segment) facts and a larger share of multi-concept composition, because that is the structure an ontology encodes and where plain text-to-SQL is weakest. Haiku's scores now inform the redesign, so validate 0.3.0's headroom on a second controlled model as well (Sonnet 5.5), to avoid fitting the benchmark to one model's blind spots. Keep 0.2.0 as the published pilot record.
