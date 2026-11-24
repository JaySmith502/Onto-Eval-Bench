# Observatory P0: design rationale

## Problem

The P0 MVP (PRD FR-01..FR-13, §22, §23) must run about three methods plus two baselines over one relational package of about 100 tasks, and publish results that a second evaluator can reproduce. Execution is the easy part. The PRD's rules are mostly rules about what may be combined: controlled results with native ones, one benchmark version with another, a candidate's finished tasks without its unfinished ones, a score without its manifest and decision. A design that stores scores and applies those rules in the report layer breaks them the first time someone adds a view. The repo was greenfield, so the only constraints were the PRD, Python 3.12, a CLI-first workflow and static reports.

## Usage (caller's view)

```sh
obs check                                                   # validate catalog/: taxonomy, candidates, configs, packages
obs plan ontoA-gpt5 relfin@1.0.0 --mode controlled --smoke  # CONFIG split only, never ranked
obs plan ontoA-gpt5 relfin@1.0.0 --mode controlled --reps 3 # -> plan file [--out F, --evaluator E], or a persisted incompatibility
obs run plans/<plan_id>.json [--evaluator jay]               # one run per plan: re-running resumes, never re-samples; seals when done
obs intervene <run_id> --kind hint --minutes 5 --note "restarted vector index"
obs score <run_id>                                          # executable checks
obs review-sample <run_id>... --rate 0.1 --seed 7           # human spot checks; disagreement => dispute
obs review <run>/<task>/<rep> --value 0.5 --rationale "..." --adjudicate   # only on a live dispute, by a third party
obs compare                                                 # one table per cohort, never merged
obs reproduce <run_id> --evaluator ana && obs run ...       # per cohort member; ana must have run none of its runs
obs void <run_id> --reason-class infrastructure --reason "..." # listed with its scores; blocks publication until...
obs confirm-void <run_id> --author lee                      # ...someone other than the voider confirms it
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

An adapter is an executable that speaks JSON over stdin and stdout with four verbs (`setup`, `build`, `run-task`, `teardown`). It is handed only staged copies of the build inputs, the task input, and scratch directories outside the catalog and the store; no request names a package or store path. The filesystem is not sandboxed, so an adapter running as the evaluator's user could still search the disk for reference answers, and every manifest records `filesystem: not_enforced`. An adapter never assigns a final status and cannot declare a task not applicable. In controlled mode every request carries the imposed model and the adapter must echo the model it used.

## Shape

Nine modules in `src/observatory/`, in data-flow order: `model` (facts and the comparability kernel) → `catalog` (authored TOML in, every check at load) → `adapter` (executable protocol, isolated invoke) → `runner` (plan and execute) → `store` (append-only files) → `scoring` (checks, reviews, verification) → `compare` (cohorts, population, statistics, coverage) → `report` (site, publish, export) → `cli`.

**Facts are appended; everything else is derived.** The records are `RunPlan`, `RunManifest`, `BuildRecord`, `AttemptStart`, `Attempt`, `Intervention`, `Seal`, `ScoringDecision`, `Incompatibility` and `Correction`, all frozen. Nothing stores a status, score or aggregate. `RunRecord.__post_init__` computes `outcomes`, which is total over planned (task, repetition), through `resolve_outcome`, the one mapping from facts to a status. `Outcome.credit` is the one place a status becomes a number. SCORED earns its verdict and every other status earns 0, so the PRD's "predefine how each status affects each metric" is one line, not a table that can drift (per single source of truth).

**Hard rules, enforced in constructors:**

- *Controlled is never ranked with native, and incompatible versions are never ranked together.* `ComparabilityKey = (exact PackageRef incl. hash, mode, track, imposed model if controlled)` is derived from the plan. `Cohort.__post_init__` rejects any run whose key differs. `compare.cohorts` returns a partition by key, and no function takes two cohorts.
- *Omitting tasks cannot improve an aggregate.* `BenchmarkPackage.eligible(track, split)` is the only definition of a plan's tasks. `Cohort` rejects any plan that lists other tasks. Every missing, failed, unsupported, timed-out or unresolved outcome stays in the denominator at 0. "Not applicable" is not a status: applicability belongs to the package, for every candidate at once, and the population never depends on any candidate's outcomes.
- *No rerun until favourable.* The run id is the plan id, so a plan has exactly one run whoever executes it; more sampling needs a new pre-registered plan, and every run of every plan pools. A cohort pools every non-voided SCORED run of a configuration, all under one `identity_hash`, and callers choose configurations, never runs. `Store.append_start` is the slot gate: no attempt outside the plan, past `repetitions`, past `max_attempts`, or after a non-`RETRYABLE` result. Only failures the runner observed (timeout, a kill by signal, a harness crash mid-attempt) are retryable; an adapter-reported failure is final. A start record precedes each invocation, so a crash mid-attempt becomes INTERRUPTED and spends the attempt. Removing a run takes a `VOID_RUN` correction with a reason class, confirmed by a second person before publication, and every report lists voided runs with their scores.
- *Stored facts keep their identity.* Each stored line carries `schema_version`, and records carry their content ids (`plan_id`, `manifest_id`, `identity_hash`, `decision_id`). The store reads a record back only if its keys are exactly the current fields and its ids still hash from its content, so an edited ledger file or a changed dataclass shape fails loudly instead of being re-identified.
- *Every score traces to a manifest and a decision.* `Outcome` refuses SCORED without `manifest_id` and `decision_id`. `ConfigSummary.trace` carries both through to the report.

**Scoring disagreement is a state the data already has.** Executable checks, reviews and adjudications are all `ScoringDecision`s. A review never supersedes anything, so a disagreeing review leaves two current decisions and the outcome resolves to UNRESOLVED. An adjudication must supersede the decisions it settles, is refused when they agree, and is refused from anyone who made one of them. A review of an executable task waits for the check's decision, and a rubric verdict must be on the rubric's scale. A check that raises is the package's bug: the outcome stays UNRESOLVED and is never charged to the candidate. UNRESOLVED blocks publication.

**Adapters are executables** (per boundary-discipline). Each verb call runs in its own process group with rlimits (address space, CPU, file size), a timeout kill, a scrubbed environment and a throwaway working directory, and talks through anonymous temporary files rather than pipes, so a stray grandchild cannot stall the harness past the timeout. A config that names a digest-pinned `container_image` runs each verb in `docker run` instead: only the call's temp cwd and the run scratch dir (read-write) and the adapter's own files (read-only) are mounted, at their host paths, so references, catalog and store do not exist inside; the network is `none` unless the config declares `network_hosts`; memory and pids are Docker limits; a timeout `docker kill`s the named container. The image and the declared hosts and secret names are part of the configuration identity (they change what is measured and what the candidate can see). The names in `secret_env` are keyed and the values never are: rotating a key must not split a cohort, while granting a different secret is a different candidate. `obs run` reads each declared name from the evaluator's environment and fails if one is unset. Every number an adapter reports is validated in `adapter.py`; a bad cost is INVALID_OUTPUT. The manifest records which isolation was actually enforced. Wire dicts never leave `adapter.py`.

**Storage is append-only JSON/JSONL in git.** Each file has one writer kind: the runner writes starts and attempts (under an exclusive run lock), the operator writes interventions, and each scorer writes its own decisions file. Readers merge (per separate-before-serializing-shared-state). Write-once files appear whole or not at all; an unterminated final log line is a crashed write, ignored on read with a warning and cut off before the next append. At MVP scale this means thousands of lines. Git provides the audit trail and the clone-to-reproduce path.

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
- We accept container isolation without a hardened runtime (no gVisor or Firecracker) and no per-host egress filtering. A config with an image records `filesystem: container`, and `network: none` or `declared_hosts_not_enforced:<hosts>` (declared hosts get the default bridge, all of it). A config without an image records `filesystem: not_enforced` and `network: not_enforced`; not handing it package or store paths stops the casual leak only. The adapter's image is trusted to be what its digest says, and the docker daemon is trusted.
- We accept that TIMEOUT and INTERRUPTED stay retryable, so an adapter that stalls on purpose can buy one more sample of a task, bounded by `max_attempts`. Making them final would charge real infrastructure flakes to the candidate.
- We accept that a check which raises on malformed output leaves that outcome UNRESOLVED (blocking publication until reviewed) instead of INVALID_OUTPUT. Checks must answer `valid=False` for output they cannot interpret.
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
  - **The plan file holds parameters, not a serialized `RunPlan`.** `obs run` re-derives the plan with `runner.plan` and refuses if its `plan_id` differs. Serialization stays inside `store.py`, and a stale plan cannot run against a changed catalog.
  - **`PackageRef.content_hash` covers every file under the package dir**, not only `package.toml`, `tasks.toml` and `checks/*`. Task inputs and references were otherwise unhashed. `packages.lock` pins a package on first load; later loads error if the hash moves.
  - **Adapter command paths** stay relative in the identity (so the identity hash is portable) and become absolute at run time against `catalog.root.parent`.
  - **Holm.** `compare` takes a bootstrap p-value per pair, `2*min(share of group-bootstrap diffs <= 0, share >= 0)`, applies Holm step-down at alpha 0.05, and demotes a directional CI call (`A_BETTER`/`B_BETTER`) to `INCONCLUSIVE` when Holm does not reject. `EQUIVALENT` is never demoted. With `multiplicity = "none"` calls are CI-only. Intervals are 95% percentile. Bootstrap seeds are strings built from `plan.seed` and the config ids, so a pair's numbers do not depend on which other configs are selected.
  - **Tag filter.** In `population` a task must carry every tag given.
  - **`coverage`** does not count MISSING outcomes as evidence for EVALUATED (a half-run run would otherwise look measured). An `Incompatibility` marks a tag UNSUPPORTED when the incompatible package has a task with that tag.
  - **Not implemented:** the per-config assessment pages of PRD §19 (the index table and the per-cohort drill-down pages carry that content). Seal log hashes are computed and checked at seal time but not re-verified on read.
  - **Export** is one `export/bundle.json`. For RESTRICTED tasks, attempt errors, output refs and decision rationales are redacted too, not only inputs, references and outputs. `report.build` and `publish` share one renderer; published pages carry no draft banner and list prior publications.

- Adversarial-review hardening (branch `harden-hard-rules`). Each exploit is a test in `tests/test_adversarial.py`. Public-surface changes, each with its reason:
  - **`RunManifest.run_id` is now a property equal to `plan.plan_id`** (the field is gone; the constructor is `RunManifest(plan, started_at, evaluator, environment, isolation)`). A free-text evaluator in the run id let anyone re-sample a plan under a new name and pool both runs. `tests/test_invariants.py` was updated for the new constructor only.
  - **`model.AttemptStart`, `Store.append_start`, `Store.open_starts`, `starts.jsonl`.** The slot gate moved from `append_attempt` to `append_start`, which also refuses a next attempt after a non-retryable result; `append_attempt` only finishes an open start. Needed so a crash mid-attempt is visible and spends the attempt. The end-to-end store test was rewritten for start-then-attempt.
  - **`RETRYABLE = {TIMEOUT, INTERRUPTED}`**; a kill by signal is now INTERRUPTED, not RUNTIME_FAILURE. An adapter could report `runtime_failure` on an answer it disliked and get a free resample.
  - **`Correction.reason_class: VoidReason | None`, `CorrectionKind.CONFIRM_VOID`, `obs void --reason-class`, `obs confirm-void`.** A void must say why, a second person must confirm it before publication, and the report lists voided runs with their scores. A silent void was a cherry-pick. The invariant test's void now states a reason class.
  - **`model.canonical`, `SCHEMA_VERSION`, `ID_FIELDS`** replace `asdict` in `content_id`; `store.to_jsonable` is `canonical(with_ids=True)`. The store's reader is now strict (exact keys, discriminated unions, ids verified). Before, a missing key took its default and `ExecutableCheck | Rubric` always read back as `ExecutableCheck`.
  - **`adapter.invoke(..., artifact_dir=...)`** replaces `workdir`. The adapter's cwd is now a throwaway temp dir, so files it writes there are not kept (the old docstring's "artifacts are collected" was a side effect of running inside the store, which also let an adapter append to `attempts.jsonl`). `stderr.log` is still kept.
  - **`adapter.AdapterNotStartable`.** A command that cannot be spawned aborts `execute` before anything is recorded; it was a candidate BUILD_FAILURE. `catalog.load` also refuses an `adapter_command` path argument that is not a file, and `Store` and `catalog.load` resolve their roots, because relative roots broke every adapter call.
  - **`AdapterRef.digest`** (set by `catalog.load` from the command's files) and **`Cohort` refusing two `identity_hash`es under one config id.** An edited config or adapter under the same id used to pool with its old runs. The fix is to give it a new id.
  - **`scoring.record_review(store, catalog, ...)`** takes the catalog, which it needs for the task's scorer kind and rubric scale. `Store.append_decision` refuses a decision whose `manifest_id` is not the run's.
  - **`Attempt.model_used`, `Attempt.adapter_version`** (defaults `None`), and `AdapterResult.model_used`. Controlled mode had no way to see a substituted model.
  - **`ConfigSummary.critical_errors` and `n_scored`** replace `critical_error_rate`. Timing out a task removed its critical error from the rate's denominator; counts beside "not scored" cannot be diluted.
  - **`scoring.CheckFault`.** `score_run` writes what it could score and then raises it; a check returning anything but a `CheckResult` raises `TypeError`. `verify` reports faulted outcomes as not reproducing.
  - **`model.check_id`** (`[A-Za-z0-9][A-Za-z0-9._-]*`) is applied by `catalog.load` (task, config, method, package ids), the CLI (people and run ids) and `Store.append_decision` (path components). Package refs must resolve inside the package dir, and a check entrypoint must name a defined function, not a prefix of one.
  - **`report.blockers`** now needs, per cohort member, a sealed, non-void REPRODUCTION run of the same identity by an evaluator who ran none of the member's runs, with no UNRESOLVED outcome and executable scoring that verifies; every member run must verify too. The end-to-end fixture now reproduces all three members. **`report.publish`** is idempotent (its id hashes the site without the history list) and renames a staged dir into place.
  - **`RunRecord.final_attempts`** is the one final-attempt rule, used by the runner, scoring and the report. **`Store.run_lock`** makes a second concurrent `execute` of a run fail. `obs compare --configs/--tags` labels its output exploratory.
  - Setup now runs on every `execute`, resumes included, so adapters must make it idempotent (stated in the adapter protocol).

## The sec-fin package

`tools/build_sec_package.py --version V` builds `catalog/packages/sec-fin/V` from one SEC Financial Statement Data Sets quarter, byte-reproducibly; each version after 0.1.0 keeps its subset rules, schema and tasks in its own module (`tools/sec_fin_0_2_0.py`, `tools/sec_fin_0_3_0.py`). 0.2.0 has 100 text-to-SQL tasks in ten scenario groups of ten, one config-split task per group, about 25/45/30 simple/moderate/challenging. Every reference is the result of a gold query stored beside it, and every task note says why the answer is unique. Tasks are never selected, dropped or tuned by how a model scores.

- **Abstention is explicit.** Ten tasks ask for a fact the database does not hold, and their reference is null. The llm-sql adapter returns null only when the query result is the single string `'NOT_IN_DATABASE'`; NULL or zero rows is a failed query (one repair turn, then INVALID_OUTPUT). The earlier convention, a single NULL row, credited any lookup that matched nothing (a wrong tag guess, say) as a correct abstention. Every question is scoped "according to the filings in this database", so the unanswerable ones carry no telltale wording, and they ask about filers in the database, not filers the subset cut.
- **Co-registrant copies are dropped.** A filing whose kept facts are identical to an earlier-accepted filing by another filer is left out before the size ranking. In 2026q3 this removes one filing, AEP TEXAS INC.'s copy of American Electric Power's 10-Q, which counted one company twice in ranks and sector counts.
- **Definitions are the publisher's.** FSDS fills the labels of a taxonomy's newest release in the quarter from filers, so `definitions.json` takes label and documentation from the newest older release (us-gaap/2025). Tags that only us-gaap/2026 has keep its documentation and get an empty label.

0.3.0 exists because 0.2.0 left too little room above the baselines (both Haiku baselines about 0.92, `docs/pilots/sec-fin-0.2.0.md`). It keeps the quarter, the filer subset and the answer check, and adds dimensional facts: the axes, members and parent/child breakdowns an ontology encodes and plain text-to-SQL handles worst. 37 of its 100 tasks are in three segment groups, and the other groups draw confounders from the same tables.

- **Dimensional facts are a second fact table, not columns on `num`.** `num` stays the consolidated, unqualified facts, so a 0.2.0-style query still means what it meant. `dnum` holds every standard-tag fact that has a segments qualifier, with `ndims` (its number of axis/member pairs), and `dim` holds one row per pair. A fact is qualified by all of its pairs, so the Cloud segment's US revenue is not the Cloud total. Co-registrant facts are the `LegalEntity` axis (FSDS's `coreg` column always equals that member, so it is not stored twice). The FSDS `segments` string is not stored: it duplicates `dim` and cost about 18 MB, and the database had to stay under 60 MB (57.9 MB). For the same reason `dnum` has no unique index (the builder deduplicates) and `dim` is `WITHOUT ROWID`.
- **Axis and member labels come from the notes data sets.** FSDS has no labels for axes or members (its `tag.txt` lists line items only, and `pre.txt` covers line items of the primary statements). The SEC's monthly Financial Statement and Notes data sets for July to September 2026 list every axis and member element in `tag.tsv`, a filer's own with the filer's label. `dimensions.json` maps each name in `dim` back to its element (the filing's own element first, then a standard one) and carries the publisher's label for standard elements, by the `definitions.json` rule, and the filer's label for its own. It is gated with the definitions by information access `definitions`, so the schema-only baseline sees only the table comments, and the context baseline retrieves the 15 best-matching labels (llm-sql-3).
- **`pre` is left out.** Its line labels are the filer's wording for primary-statement lines, which would help both baselines find consolidated facts equally and says nothing about axes or members.

## Open questions and risks

- How many scenario groups will the first package have? Below `min_groups` every pairwise call reads INCONCLUSIVE. Is that the honest pilot output you want, or should groups be finer?
- Should a controlled-mode UNSUPPORTED instead become a plan-time `Incompatibility`?
- What is `min_meaningful_diff` for the first package, and who fixes it before the first scored run?
- Do second-evaluator REPRODUCTION runs stay out of rankings (as sketched) and get reported as an agreement view?
- Is container isolation without per-host egress filtering acceptable for the first public run? A candidate that declares hosts can reach any host, so the model API key it is given can be sent anywhere. The upgrade is an egress proxy container with an allowlist, which the adapter network would be restricted to. Configs with no image remain unisolated and should not be used for held-out scoring.

## Next implementation step

Build the SEC FSDS vertical slice (`sec-fin` package and the two Haiku baseline configs, containerized), run the schema-only and context-retrieval baselines through `obs plan` and `obs run` with `ANTHROPIC_API_KEY` in `secret_env`, and read the first cohort comparison. Then decide on the egress proxy from the open question above.
