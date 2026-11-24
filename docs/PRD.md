PRD: Ontology Evaluation Observatory
Version: 0.2
Product owner: Jay Smith
Status: Draft for validation
Working name: Ontology Evaluation Observatory

1. Product summary
Build an independent evaluation platform and publication that assesses ontology platforms and methods using transparent, repeatable, evidence-backed procedures.

The product serves two complementary purposes:

Public assessment: evaluate new platforms, methods, and releases; publish their capabilities, limitations, and measured trade-offs.

Private client selection: evaluate suitable candidates against a client’s confidential data, tasks, and deployment requirements.

The central question is:

Which platform or method works well for which workloads, under what conditions, at what cost—and how strong is the evidence?

The product will combine a versioned benchmark collection, extensible execution framework, task-level evidence repository, and public reporting interface.

It will not reduce every platform to one universal score. Comparisons will reflect capability coverage, evaluation conditions, uncertainty, and practical constraints.

2. Problem statement
The evaluation process must address several problems identified during product planning:

“Ontology platform” describes systems with different capabilities and intended uses.

A plausible ontology does not necessarily improve downstream task outcomes.

Results depend on the underlying model, configuration, available information, resource budget, and human assistance.

Success on one dataset does not establish suitability for another client.

Public test sets can become familiar to developers and systems.

Scores become stale as platforms, models, data, and benchmark suites change.

Clients need evidence about cost, maintenance, security, and deployment—not only task accuracy.

Readers need to distinguish meaningful differences from measurement noise.

The product must make these limitations visible rather than hide them behind a ranking.

3. Goals and non-goals
Goals
Provide a neutral assessment source for emerging ontology methods and platforms.

Make evaluations repeatable, inspectable, and version-specific.

Show which capabilities and workload types were—and were not—tested.

Measure whether an ontology method adds value over appropriate alternatives.

Separate controlled method comparisons from native end-to-end platform comparisons.

Support defensible client-specific selection.

Preserve historical results and detect regressions.

Maintain transparent editorial, funding, and correction policies.

Non-goals
Declare a universal best ontology platform.

Recommend a solution from data volume or industry alone.

Treat human preference as proof of correctness.

Treat a conformance test as a complete platform evaluation.

Certify regulatory compliance.

Publish confidential client artifacts without permission.

Support every platform, industry, modality, and scale at launch.

Build automated platform selection before sufficient evaluation evidence exists.

4. Users and jobs to be done
User	Job	Primary output
Technical buyer or architect	Understand trade-offs among viable solutions	Filterable comparisons and evidence
Client sponsor	Select a solution for private requirements	Confidential recommendation
Researcher or practitioner	Evaluate a method’s contribution	Reproducible assessment and failure analysis
Platform developer	Reproduce and respond to findings	Run details and correction process
Evaluator or editor	Assess new releases consistently	Structured evaluation and publication workflow
5. Guiding principles
Evidence before ranking. Measured claims must trace to tasks, runs, and scoring rules.

Coverage before generalization. Conclusions must stay within the evaluated workload space.

Comparable conditions. Rankings must use declared common tasks and compatible evaluation conditions.

Separate utility from artifact quality. Ontology quality and downstream performance are distinct outcomes.

Transparent resource use. Cost, tuning, retries, and human intervention are part of the result.

Explicit uncertainty. Close results may be inconclusive.

Historical integrity. Preserve prior results and disclose material changes.

Independence. Commercial relationships cannot purchase favorable findings or suppress unfavorable ones.

Client confidentiality. Private data and evaluation artifacts remain private by default.

6. Unit of evaluation
A result belongs to a specific evaluated configuration—not a platform brand in isolation.

Every result must identify:

Platform or method version + underlying model + configuration + information access + resource budget + benchmark version + execution environment.

Additional required identifiers include:

Adapter version.

Evaluation mode.

Execution date.

Relevant service endpoint or deployment.

Scorer and judge versions.

Human interventions.

Repetition count.

Any unresolved version ambiguity.

When a provider does not expose immutable versions, label the result as a dated service observation with limited reproducibility.

7. Evaluation tracks and modes
Evaluation tracks
Track	Central question	Example outcomes
Ontology construction	Does the method build an accurate, useful semantic model?	Concept coverage, unsupported assertions, expert judgments
Semantic task performance	Does the ontology improve successful task completion?	Correct outcomes, critical errors, appropriate abstention
Interoperability and conformance	Does the system preserve required semantics across supported interfaces?	Construct support, validation, round-trip fidelity
Robustness	Does performance survive controlled input changes?	Performance degradation, failure categories
Evolution and maintenance	Can the system adapt without damaging existing behavior?	Update effort, improvement, regressions
Operational fit	Can the solution meet practical deployment requirements?	Cost, latency, setup effort, deployment constraints
Evaluation modes
Mode	Controls	Purpose
Controlled method	Common model, agent, tools, information access, and budget where feasible	Isolate the method’s contribution
Native platform	Common tasks and declared constraints; supported native stack	Compare complete solutions
Private client	Client eligibility gates and confidential benchmark package	Support a specific selection decision
Do not combine these modes into a shared ranking.

If a platform cannot satisfy a controlled configuration, report the incompatibility rather than present a native result as equivalent.

8. Coverage taxonomy
Maintain a versioned map of the intended evaluation space and actual coverage.

Axis	Example categories
Data format	Relational tables, documents, structured files, mixed sources, event data
Data quality	Clean, incomplete, noisy, inconsistent, conflicting
Semantic structure	Definitions, hierarchies, relationships, business rules, temporal meaning
Task	Construction, querying, explanation, alignment, correction, updating
Complexity	Source count, schema size, entity count, cross-source relationships
Change	Static inputs, incremental updates, schema changes
Domain	Industry and business-function tags
Operations	Deployment model, access controls, concurrency, resource constraints
The public coverage matrix must distinguish:

Evaluated.

Supported but not evaluated.

Unsupported.

Unknown.

Not applicable.

Industry labels are descriptive metadata. Recommendations must also consider structural workload characteristics.

9. Benchmark suite architecture
Use complementary suites rather than one continually changing test set.

Suite	Purpose
Frozen core	Stable regression and longitudinal comparisons
Fresh challenge	Newly created tasks and data scenarios
Perturbation	Controlled changes to test robustness
Evolution	Controlled updates and adaptation
Private client	Confidential client selection and validation
Release and freshness policy
Assign a version to every benchmark package and suite release.

Preserve historical tasks, scoring rules, and results.

Publish a refresh policy and release changelog.

Re-run a reference cohort after material benchmark changes.

Do not directly rank scores from incompatible benchmark versions.

Keep tuning examples separate from held-out evaluation tasks.

Describe exposure risks without claiming that freshness proves absence of contamination.

Define task retirement rules for ambiguity, leakage, saturation, or invalid reference answers.

Openly reproducible and restricted held-out suites may coexist. Reports must identify which evidence can be independently reproduced.

10. Benchmark package specification
Each package must contain:

Identifier, version, provenance, and usage rights.

Data snapshot identifiers and integrity checks.

Coverage taxonomy tags.

Tasks and explicit success criteria.

Reference results, executable checks, or expert rubrics.

Task difficulty and consequence labels.

Dataset or scenario grouping for statistical analysis.

Configuration and held-out splits.

Permitted inputs, external access, tools, and human assistance.

Resource budgets, retry rules, and timeouts.

Scoring and adjudication rules.

Known limitations and ambiguities.

Disclosure and publication restrictions.

A client’s proposed 5 GB dataset is an input workload, not a complete benchmark. It must be accompanied by representative tasks, reference outcomes, and requirements.

11. Core workflows
New-platform assessment
Register the platform, release, and attributed claims.

Classify capabilities and applicable tracks.

Review access, licensing, execution risks, and cost.

Implement or validate an adapter.

Select compatible benchmark packages and baselines.

Freeze evaluation conditions and scoring rules.

Run smoke tests, then the scored evaluation.

Review failures, resource use, and scoring disagreements.

Run additional repetitions or tasks when justified.

Offer time-limited developer factual review.

Publish findings, evidence, disclosures, and limitations.

Schedule or trigger re-evaluation.

Developer review permits factual correction, not editorial veto.

Client evaluation
Capture business tasks and nonnegotiable requirements.

Approve data handling, external processing, and retention.

Profile the agreed data snapshot.

Develop and review evaluation tasks.

Separate configuration and held-out examples.

Apply eligibility gates.

Screen compatible candidates.

Evaluate finalists more deeply.

Produce a recommendation with uncertainty and sensitivity analysis.

Retain or delete artifacts according to the agreement.

12. Functional requirements
P0 is required for the public research MVP. P1 expands evaluation depth or enables private-client delivery. P2 covers later automation.

ID	Requirement	Priority
FR-01	Maintain a platform, method, version, and capability registry.	P0
FR-02	Manage versioned benchmark packages and coverage tags.	P0
FR-03	Provide adapters for setup, build, execution, artifact collection, and teardown; expose updates where supported.	P0
FR-04	Capture complete run manifests and human interventions.	P0
FR-05	Execute applicable baselines under declared conditions.	P0
FR-06	Preserve task-level outputs, errors, timing, cost evidence, and scoring decisions.	P0
FR-07	Support executable scoring and documented human adjudication.	P0
FR-08	Enforce resource limits, retry policies, and execution isolation.	P0
FR-09	Enforce comparability and missing-result rules.	P0
FR-10	Generate assessments, common-task comparisons, and coverage matrices.	P0
FR-11	Publish benchmark versions, disclosures, corrections, and historical results.	P0
FR-12	Report uncertainty and support paired comparisons.	P0
FR-13	Support exportable manifests and results where disclosure permits.	P0
FR-14	Provide blind expert-review workflows.	P1
FR-15	Support robustness, evolution, and regression suites.	P1
FR-16	Support fixed-budget and quality-target comparisons.	P1
FR-17	Support secure private benchmarks and client reports.	P1
FR-18	Provide cost-quality and release-comparison views.	P1
FR-19	Support operational load testing.	P2
FR-20	Generate evidence-based shortlists with explanations and uncertainty.	P2
13. Baselines and ablations
Use an applicable baseline ladder to investigate where improvements originate:

Schema or metadata only.

Simple retrieval of relevant business definitions.

Automatically constructed ontology.

Human-corrected ontology.

Evolved ontology after a controlled adaptation phase.

Not every candidate must support every level.

For each comparison, disclose:

Available information.

Underlying model and agent.

Build and inference budgets.

Expert assistance.

Differences that prevent causal attribution.

The evaluation should distinguish benefits attributable to semantic structure from benefits attributable to additional information, compute, or human effort.

14. Comparability and aggregation rules
Common-task comparisons
Rank candidates only over a declared common task set.

Display task counts, dataset counts, and coverage beside scores.

Recompute common-subset results when users select different candidates.

Explain when filtering changes the comparison population.

Keep controlled and native results separate.

Outcome classification
Record distinct statuses for:

Completed and scored.

Unsupported capability.

Setup or build failure.

Runtime failure.

Timeout.

Invalid output.

Missing result.

Unscored or disputed.

Not applicable.

Predefine how each status affects each metric. Candidates must not improve their aggregate merely by omitting difficult eligible tasks.

Aggregation
Publish task and category weighting.

Report task-level and dataset-level summaries where appropriate.

Do not publish a default universal composite score.

If clients request weighted scoring, disclose weights and show sensitivity to reasonable alternatives.

Treat end-to-end build failure as an outcome in the native-platform track; keep any component-level findings separately labeled.

15. Metrics and measurement boundaries
Outcome metrics
Dimension	Measures
Task correctness	Success rate, partial credit under explicit rubrics, critical error rate
Grounding	Supported claims, traceable evidence, unsupported assertions
Abstention	Appropriate abstentions and incorrect refusals
Ontology quality	Required concept/relation coverage, validity, expert assessment
Robustness	Change from clean-condition performance
Stability	Variation across runs and releases
Maintenance	Update effort, new-task improvement, retained performance
Resource metrics
Measure separately:

Human setup and correction time.

Unattended build time.

Time to a usable ontology under a defined acceptance threshold.

End-to-end task completion time.

Warm and cold query latency.

Update time.

Model/API usage.

Compute and storage usage where observable.

Retry and failed-run costs.

Recurring platform costs and relevant licensing assumptions.

Distinguish measured costs from estimates. State what each estimate includes.

Comparison views
Support:

Native/default configuration.

Performance within a fixed resource budget.

Cost required to reach a declared quality target.

Cost-quality frontier.

Do not extrapolate quality-target costs when the target was not reached.

16. Scoring and statistical policy
Prefer executable checks when appropriate.

Score correctness independently of wording or query syntax where possible.

Use documented human review for ambiguous cases.

Version model-based judges and test their agreement with reviewed judgments.

Apply equivalent scoring rules across candidates.

Compare candidates on the same tasks using paired methods.

Account for related tasks from the same dataset or scenario.

Report uncertainty for differences as well as individual scores.

Define practically meaningful improvements before examining final results.

Label unsupported conclusions or close differences as inconclusive.

Predefine rules for additional sampling to avoid selectively rerunning favorable outcomes.

Disclose exploratory analyses and multiple-comparison limitations.

Reports must state both the number of tasks and the diversity of datasets or scenarios represented.

17. Blind expert review
Use blind review for artifact qualities that cannot be adequately captured by executable checks.

Workflow
Remove platform identifiers where feasible.

Present identical source material and rubrics.

Randomize candidate order.

Offer both original artifacts and normalized views where practical.

Allow “A better,” “B better,” “tie,” and “both unacceptable.”

Require reasons for judgments.

Record reviewer expertise, conflicts, and agreement.

Adjudicate material disagreements.

Review dimensions may include conceptual clarity, abstraction, business fidelity, maintainability, and correction effort.

Expert preference scores must remain separate from task correctness.

18. Robustness, evolution, and operational tests
Robustness
Supported perturbations may include:

Meaning-preserving table or column renaming.

Question paraphrasing.

Irrelevant source additions.

Missing metadata.

Conflicting business definitions.

Changes in source organization.

Validate whether each transformation preserves or intentionally changes the expected answer.

Evolution
Freeze initial-build results.

Permit a declared adaptation budget.

Evaluate new tasks on unseen examples.

Re-run prior tasks to detect regressions.

Track correction effort and update cost.

Operations
Later-stage load tests should measure concurrency, throughput, error rate, median and tail latency, and resource use under declared load profiles.

19. Public interface and assessment reports
Interface
The public product must provide:

Platform and configuration cards.

Capability coverage matrix.

Common-task comparison controls.

Task and failure drill-down.

Benchmark-version and freshness labels.

Visible uncertainty and sample sizes.

Methodology, disclosures, and correction history.

Exportable evidence where permitted.

Later releases add cost-quality charts, blind-review tools, release comparisons, and client-weighted views.

Report template
Every assessment includes:

Exact evaluated configuration.

Attributed platform claims.

Independently observed findings.

Tested and excluded workloads.

Baseline comparisons.

Results by task category.

Representative failures.

Cost, latency, and human effort.

Statistical uncertainty.

Reproducibility details and restrictions.

Funding and assistance disclosures.

Limitations and revision history.

Conclusions must use bounded language: “performed best on this suite under these conditions,” not “best ontology platform.”

20. Independence and governance
Launch policies must establish that:

Payment cannot purchase favorable findings, rankings, or suppression.

Sponsorships, credits, affiliations, and vendor assistance are disclosed.

Paid client work does not control public conclusions.

Inclusion and prioritization criteria are public.

Assisted and unassisted runs are distinguishable.

Tuning budgets and exceptions are documented.

Developers have a factual-review and dispute route.

Material corrections preserve an audit trail.

Declining participation is not labeled a measured failure.

Paid re-evaluation does not guarantee a favorable result.

21. Security and confidential data
Before private-client delivery, support:

Tenant separation and least-privilege access.

Encryption in transit and at rest.

Explicit authorization for external processing.

Retention and deletion controls.

Secrets management and redaction.

Sandboxed execution and restricted network access.

Appropriate query permissions.

Auditable data access.

Client approval before publication.

Private data must not become training material, public benchmark content, or cross-client examples by default.

22. Proposed MVP
Area	Scope
Candidates	Three methods with a genuinely comparable task
Data	One public relational-data package with business definitions
Tasks	Approximately 100 reviewed tasks, subject to pilot validation
Baselines	Metadata-only and simple business-context retrieval
Modes	Controlled comparisons where feasible; native results separately labeled
Execution	CLI-first runner, adapters, manifests, resource limits
Reporting	Assessments, common-task table, coverage map, task evidence
Statistics	Paired comparisons and uncertainty reporting
Scale check	Separate feasibility test around the proposed 5 GB workload
The initial package demonstrates workflow repeatability, not broad industry coverage or general scalability.

23. Release acceptance criteria
The MVP may publish when:

Every score traces to a complete manifest and scoring decision.

Every task has a reference check or documented rubric.

Common-task and missing-result rules are enforced.

Resource boundaries and interventions are visible.

Coverage and exclusions are explicit.

A second evaluator reproduces a sample of the process.

Scoring disagreements are reviewed.

Reports show uncertainty, limitations, and disclosures.

Historical results and corrections remain accessible.

Execution respects configured runtime and spending limits.

24. Success measures
Track:

Onboarding time and effort per candidate.

Reproducibility-record completeness.

Execution failure and scoring-dispute rates.

Automated-versus-reviewed scoring agreement.

Cost per assessment.

Workload coverage and concentration.

Time from material release to updated assessment.

Independent reproduction attempts and outcomes.

Client decision usefulness.

Correction frequency and resolution time.

Set numerical targets after the pilot establishes realistic baselines.

25. Delivery sequence
Phase	Deliverable
1. Methodology	Taxonomy, benchmark schema, comparability rules, resource definitions, independence charter
2. Pilot	Adapters, baselines, reviewed tasks, scoring validation, reproduction exercise
3. Public launch	First cohort, evidence-backed reports, coverage map, correction process
4. Evaluation depth	Blind review, robustness, evolution, cost-quality comparisons
5. Private clients	Secure onboarding, confidential benchmarks, recommendations
6. Expansion	New workload families, load testing, evidence-based shortlisting
26. Principal risks
Risk	Mitigation
Risk	Mitigation
Incompatible products appear comparable	Tracks, modes, capability gates
Underlying model dominates the result	Controlled comparisons and ablations
Missing tasks inflate scores	Common-task rules and explicit statuses
Public tasks become overfamiliar	Fresh suites, held-out tasks, exposure disclosures
Rankings overstate weak evidence	Uncertainty, coverage limits, inconclusive labels
Expert review rewards presentation	Blinding, normalized views, separate correctness
Commercial pressure damages trust	Funding and editorial policies
Costs grow faster than coverage	Screening, staged evaluation, hard budgets
Version changes invalidate conclusions	Configuration identity and historical continuity
27. Initial product decision
Build the evaluation standard, execution workflow, and evidence trail before investing in automated recommendations or a sophisticated ranking interface.

The first milestone is:

A second evaluator can apply the published process to several comparable methods, reproduce selected findings, inspect the reasons for differences, and repeat the assessment after a new release.

