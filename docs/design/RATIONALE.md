# Observatory P0: design rationale

## Problem

The P0 MVP (PRD FR-01..FR-13, §22, §23) must run about three methods plus two baselines over one relational package of about 100 tasks, and publish results that a second evaluator can reproduce. Execution is the easy part. The PRD's rules are mostly rules about what may be combined: controlled results with native ones, one benchmark version with another, a candidate's finished tasks without its unfinished ones, a score without its manifest and decision. A design that stores scores and applies those rules in the report layer breaks them the first time someone adds a view. The repo was greenfield, so the only constraints were the PRD, Python 3.12, a CLI-first workflow and static reports.

## Usage (caller's view)

```sh
obs check                                                   # validate catalog/: taxonomy, candidates, configs, packages
obs plan ontoA-gpt5 relfin@1.0.0 --mode controlled --smoke  # CONFIG split only, never ranked
obs plan ontoA-gpt5 relfin@1.0.0 --mode controlled --reps 3 # -> plan file [--out F, --evaluator E], or a persisted incompatibility
obs run plans/<plan_id>.json [--evaluator jay]               # resumable; seals the run when done (plan file = parameters + plan_id; re-derived)
obs intervene <run_id> --kind hint --minutes 5 --note "restarted vector index"
obs score <run_id>                                          # executable checks
obs review-sample <run_id>... --rate 0.1 --seed 7           # human spot checks; disagreement => dispute
obs review <run>/<task>/<rep> --value 0.5 --rationale "..." --adjudicate
obs compare                                                 # one table per cohort, never merged
obs reproduce <run_id> --evaluator ana && obs run ...       # second evaluator (must differ from the original's)
obs verify <run_id>                                         # executable scoring reproduces exactly?
obs report site/                                            # draft site + export bundle
obs publish                                                 # refuses and lists blockers until clean (report.PublishBlocked)
# global options go before the command: --catalog DIR (default catalog), --store DIR (default store)
```

```python
cat = catalog.load(Path("catalog"))
p = runner.plan(store, cat, config_id="ontoA-gpt5", package="relfin@1.0.0", mode=Mode.CONTROLLED)
if isinstance(p, RunPlan):
    run_id = runner.execute(store, cat, p, evaluator="jay")
    scoring.score_run(store, cat, run_id)

for key, cohort in compare.cohorts(store, cat).items():      # a partition: mixing is not expressible
    c = compare.compare(cohort, tags=frozenset({"task:querying"}))
    for pair in c.pairs:
        print(key.mode, key.package, pair.a, pair.b, pair.diff, pair.call)
```

An adapter is an executable that speaks JSON over stdin and stdout with four verbs (`setup`, `build`, `run-task`, `teardown`). It never sees reference answers, never assigns a final status, and cannot declare a task not applicable.

## Shape

Nine modules in `src/observatory/`, in data-flow order: `model` (facts and the comparability kernel) → `catalog` (authored TOML in, every check at load) → `adapter` (executable protocol, isolated invoke) → `runner` (plan and execute) → `store` (append-only files) → `scoring` (checks, reviews, verification) → `compare` (cohorts, population, statistics, coverage) → `report` (site, publish, export) → `cli`.

**Facts are appended; everything else is derived.** The records are `RunPlan`, `RunManifest`, `BuildRecord`, `Attempt`, `Intervention`, `Seal`, `ScoringDecision`, `Incompatibility` and `Correction`, all frozen. Nothing stores a status, score or aggregate. `RunRecord.__post_init__` computes `outcomes`, which is total over planned (task, repetition), through `resolve_outcome`, the one mapping from facts to a status. `Outcome.credit` is the one place a status becomes a number. SCORED earns its verdict and every other status earns 0, so the PRD's "predefine how each status affects each metric" is one line, not a table that can drift (per single source of truth).

**Hard rules, enforced in constructors:**

- *Controlled is never ranked with native, and incompatible versions are never ranked together.* `ComparabilityKey = (exact PackageRef incl. hash, mode, track, imposed model if controlled)` is derived from the plan. `Cohort.__post_init__` rejects any run whose key differs. `compare.cohorts` returns a partition by key, and no function takes two cohorts.
- *Omitting tasks cannot improve an aggregate.* `BenchmarkPackage.eligible(track, split)` is the only definition of a plan's tasks. `Cohort` rejects any plan that lists other tasks. Every missing, failed, unsupported, timed-out or unresolved outcome stays in the denominator at 0. "Not applicable" is not a status: applicability belongs to the package, for every candidate at once, and the population never depends on any candidate's outcomes.
- *No rerun until favourable.* A cohort pools every non-voided SCORED run of a configuration, and callers choose configurations, never runs. `Store.append_attempt` is slot-gated: no attempt outside the plan, past `repetitions`, or past `max_attempts`. Retries happen only for `RETRYABLE` infrastructure failures. Removing a run takes a public `VOID_RUN` correction.
- *Every score traces to a manifest and a decision.* `Outcome` refuses SCORED without `manifest_id` and `decision_id`. `ConfigSummary.trace` carries both through to the report.

**Scoring disagreement is a state the data already has.** Executable checks, reviews and adjudications are all `ScoringDecision`s. A review never supersedes anything, so a disagreeing review leaves two current decisions and the outcome resolves to UNRESOLVED. An adjudication must supersede the decisions it settles. UNRESOLVED blocks publication.

**Adapters are executables** (per boundary-discipline). Each verb call runs in its own process group with rlimits, a timeout kill, a scrubbed environment and its own working directory. The manifest records which isolation was actually enforced. Wire dicts never leave `adapter.py`.

**Storage is append-only JSON/JSONL in git.** Each file has one writer kind: the runner writes attempts, the operator writes interventions, and each scorer writes its own decisions file. Readers merge (per separate-before-serializing-shared-state). At MVP scale this means thousands of lines. Git provides the audit trail and the clone-to-reproduce path.

**Statistics are pre-registered.** `AnalysisPlan` (margin, resamples, seed, `min_groups`, multiplicity) sits inside the package hash. `compare.compare` takes no tuning arguments. It uses a paired cluster bootstrap over the task groups. With fewer than `min_groups` groups every call is INCONCLUSIVE. A task-level interval is shown beside it as a sensitivity figure only.

**Interface depth.** The public surface is about twelve functions: `catalog.load`, `runner.plan/execute/reproduce`, `scoring.score_run/record_review/review_sample/verify`, `compare.cohorts/compare/coverage`, and `report.build/publish/export`. Behind it sit process isolation, retries, spend caps, resume, status resolution, dispute handling, the comparability checks, the bootstrap and redaction. Callers cannot pass a task list, budget, status, score or seed.

**Deliberately absent:** a composite score, a rank field, cross-cohort tables, a database, model-based judges, private-client mode, network sandboxing, and blind review (P1).

## Synthesis decision

Four runners: opus (A), fable (B), sonnet (C), haiku (D). All four converged on the same skeleton: an append-only file ledger, derived status, a `Cohort` keyed on benchmark digest and mode, a package-fixed denominator, subprocess adapters and a cluster bootstrap. That convergence is strong agreement on the shape.

- **Base: A (opus).** Cross-judge (fable) scores were A 25, B 24, C 19, D 15. A had the most complete system around the kernel: the catalog lockfile, plan-time `Incompatibility`, `Seal`, run `Purpose` (smoke, scored, reproduction), publish blockers and immutable publications.
- **From B (fable), the kernel:** `Status` carries who may assign it (`Origin`); outcomes are computed in the `RunRecord` constructor; `Outcome.credit` is the only status-to-number step; disputes are expressed through supersession, not a workflow object. B's `ComparabilityKey` keys the imposed model in controlled mode and only discloses information access, so the baseline ladder shares a cohort. This replaces A's `Cell`/`CELL_SCORE`, where a hand-built `Cell(status=TIMEOUT, score=1.0)` passed validation.
- **Rejected from B:** `Treatment.DROPS_TASK`. The judge confirmed by running it that a registry NOT_APPLICABLE on one config dropped the task for every candidate and raised a rival's aggregate from 0.75 to 1.0. Package-owned eligibility (A, D) replaces it, which leaves the credit rule one line long.
- **From C (sonnet):** slot-gated attempt writes, plus `min_groups` (forced INCONCLUSIVE) and multiplicity in the pre-registered `AnalysisPlan`. These replace A's task-bootstrap fallback, which understated uncertainty.
- **From D (haiku):** `verify` (re-score without writing and compare decision ids) as the mechanical reproduction check, the task-level interval as a sensitivity figure, and an `assisted` flag derived from interventions.
- **Rejected:** C's per-mode status-treatment table (speculative) and its `ResolvedCell` mint sentinel (forgeable in Python). D's caller-supplied outcomes in `make_group` (allowed cherry-picking).

## Tradeoffs accepted

- We accept zero credit for UNSUPPORTED in both modes, which penalises narrow tools, in exchange for an aggregate that can't be gamed. Status counts beside every score show why a number is low.
- We accept exact package-hash equality as the comparability rule: a one-task typo fix splits the cohort. In exchange there is a rule nobody can argue about. Re-running the reference cohort is what the PRD asks for anyway.
- We accept that all members share the full eligible set, so there is no per-candidate "common subset". The only population change is a tag filter applied to everyone. This may look like a skipped PRD §14 requirement, but with package-owned eligibility the common subset is the eligible set.
- We accept that UNRESOLVED scores 0 in drafts in exchange for drafts that are always computable. Publication refuses while any outcome is unresolved.
- We accept no network isolation in the MVP. Each manifest records `network: not_enforced`.
- We accept running package checks in process (trusted and hash-versioned). Untrusted adapter output reaches checks only as data.
- We accept a full scan on every read in exchange for having no index to keep in sync.

## Alternatives considered

- **SQLite with stored score rows.** It hides storage but exposes the rules: every query must remember the mode and version predicates, and stored scores can outlive their decisions. It also loses git diffs and clone-to-reproduce.
- **In-process Python adapter classes.** A smaller surface, but timeouts become cooperative, adapters can import internals and reference answers, and only Python works.
- **Candidate-declared applicability with an intersection common set.** This is the literal reading of PRD §14, and it is the omission loophole, so it was rejected.
- **Separate pipelines per mode.** Mixing modes becomes a type error, but every signature is duplicated. A key field gives the same guarantee once.

## Implementation reconciliation

- `model.resolve_outcome` and `RunRecord.__post_init__` are implemented. `tests/test_invariants.py` has 10 tests, all passing. During implementation the unused `sealed` parameter was dropped: unreached cells of an unsealed run are already MISSING because they have no attempt. Unsealed runs block at `report.blockers`.
- Phase D implemented every `NotImplementedError` body. `tests/test_end_to_end.py` runs plan, execute, score, compare, report and publish over `tests/fixtures/`, once through the library and once through `cli.main`. Accepted deviations, each with its reason:
  - **`AdapterResult.error: str = ""`** (new field). The sketch had nowhere to carry the failure text that `Attempt.error` needs. Requirement missed by the sketch.
  - **`adapter.isolation(budget)`** (new function). The manifest is written before the first adapter call, yet must record what was enforced. It probes the limits in a throwaway child (a pipe written from `preexec_fn`) and `runner.execute` stores the result. On macOS `RLIMIT_AS` is reported `not_enforced`; `RLIMIT_CPU` is enforced. Sketch missed the ordering.
  - **`Store.exists(run_id)`** (new). Prefix matching in `Store.run` could return `X-jay2` for a missing `X-jay`; the runner needs an exact test. **`store.to_jsonable`** (new, public). `report.export` must dump dataclasses, and the serialization logic stays in `store.py` instead of being copied. Sketch missed export's need.
  - **`report.PublishBlocked`** (new exception). `publish` returns a path, so refusal needs a typed carrier for the blocker list.
  - **Run id is `<plan_id>-<evaluator>`.** The sketch did not say how "existing unsealed run for plan.plan_id" is found. A second `execute` of the same plan by the same evaluator resumes or returns the sealed run, so a rerun cannot re-sample. A second evaluator gets a separate run.
  - **The plan file holds parameters, not a serialized `RunPlan`.** `obs run` re-derives the plan with `runner.plan` and refuses if its `plan_id` differs. Serialization stays inside `store.py`, and a stale plan cannot run against a changed catalog.
  - **`PackageRef.content_hash` covers every file under the package dir**, not only `package.toml`, `tasks.toml` and `checks/*`. Task inputs and references were otherwise unhashed. `packages.lock` pins a package on first load; later loads error if the hash moves.
  - **Adapter command paths** stay relative in the identity (so the identity hash is portable) and become absolute at run time against `catalog.root.parent`.
  - **Holm.** `compare` takes a bootstrap p-value per pair, `2*min(share of group-bootstrap diffs <= 0, share >= 0)`, applies Holm step-down at alpha 0.05, and demotes a directional CI call (`A_BETTER`/`B_BETTER`) to `INCONCLUSIVE` when Holm does not reject. `EQUIVALENT` is never demoted. With `multiplicity = "none"` calls are CI-only. Intervals are 95% percentile. Bootstrap seeds are strings built from `plan.seed` and the config ids, so a pair's numbers do not depend on which other configs are selected.
  - **Tag filter.** In `population` a task must carry every tag given.
  - **`critical_error_rate`** is over SCORED outcomes (a critical error is a property of a verdict); status counts show the rest.
  - **`coverage`** does not count MISSING outcomes as evidence for EVALUATED (a half-run run would otherwise look measured). An `Incompatibility` marks a tag UNSUPPORTED when the incompatible package has a task with that tag.
  - **Not implemented:** `INTERRUPTED` attempts (a crash-lost in-flight attempt is simply re-run, since no start marker is written); the per-config assessment pages of PRD §19 (the index table and the per-cohort drill-down pages carry that content); the catalog check "identity hashes unique per slug" (config id is the file name and part of the hash, so it cannot collide). Seal log hashes are computed and checked at seal time but not re-verified on read.
  - **Checks that raise** make that output INVALID_OUTPUT with the exception as rationale, because untrusted output may break a check.
  - **Export** is one `export/bundle.json`. For RESTRICTED tasks, attempt errors, output refs and decision rationales are redacted too, not only inputs, references and outputs. `report.build` and `publish` share one renderer; published pages carry no draft banner and list prior publications.

## Open questions and risks

- How many scenario groups will the first package have? Below `min_groups` every pairwise call reads INCONCLUSIVE. Is that the honest pilot output you want, or should groups be finer?
- Should a controlled-mode UNSUPPORTED instead become a plan-time `Incompatibility`?
- What is `min_meaningful_diff` for the first package, and who fixes it before the first scored run?
- Do second-evaluator REPRODUCTION runs stay out of rankings (as sketched) and get reported as an agreement view?
- Is process-level isolation acceptable for the first public run, or do candidate platforms need network restriction from day one?

## Next implementation step

Implement `store.py` serialization and `catalog.load` against a three-task fixture package, then `runner.execute` against a fake adapter script, so that one run round-trips through disk into a `Cohort`.
