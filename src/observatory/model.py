"""Domain vocabulary and the comparability kernel. Pure: no I/O.

Reading order: Status -> registry/identity -> benchmark package -> ledger
records (facts, append-only) -> derived views (Outcome, RunRecord, Cohort).

Facts are frozen and only ever appended. Outcome, RunRecord.outcomes and
Cohort are recomputed on every read and never stored, so no stored aggregate
can drift from its evidence.

The PRD's hard rules live here, enforced in constructors whoever calls them:
- A Cohort has one ComparabilityKey (exact package ref, mode, track, and the
  common model in controlled mode). Controlled and native results, or two
  package versions, cannot share a Cohort.
- Task eligibility is a property of the package. A RunPlan must list exactly
  the package's eligible tasks, and RunRecord.outcomes is total over
  (task, repetition): an absent result is MISSING at zero credit. Omitting a
  task can only lower an aggregate.
- A SCORED outcome cannot exist without a manifest id and a decision id.
- A Cohort holds every non-voided SCORED run of each member; callers choose
  configurations, never runs.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime

METHODOLOGY_VERSION = "1"
"""Bump when the status->credit rule or statistics change. Stamped on every report."""

SCHEMA_VERSION = "2"
"""Bump when any stored record's fields change. Stamped on every stored line and mixed into every
content id, so records of another shape are refused on read instead of silently re-identified."""

ID_FIELDS = {"RunPlan": "plan_id", "RunManifest": "manifest_id", "ConfigurationIdentity": "identity_hash",
             "ScoringDecision": "decision_id"}
"""Content ids the store writes beside each record and verifies when it reads the record back."""


class Incomparable(ValueError):
    """Results that must not share a ranking were put into one Cohort."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def check_id(kind: str, value: str) -> str:
    """Ids (task, config, method, package, run, people) become path components; anything else is
    refused where it enters (catalog, CLI, store paths)."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{kind} {value!r} must match {_ID.pattern}")
    return value


def canonical(o: object, *, with_ids: bool = False) -> object:
    """The one projection of facts to JSON values: what content ids hash, and (with_ids) what the store
    writes. Init fields only; enums by value (Status by name); sets sorted."""
    if is_dataclass(o) and not isinstance(o, type):
        d = {f.name: canonical(getattr(o, f.name), with_ids=with_ids) for f in fields(o) if f.init}
        if with_ids and (k := ID_FIELDS.get(type(o).__name__)):
            d[k] = getattr(o, k)
        return d
    if isinstance(o, Status):
        return o.name
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(canonical(x, with_ids=with_ids) for x in o)
    if isinstance(o, Mapping):
        keys = ((canonical(k), v) for k, v in o.items())
        return {"/".join(map(str, k)) if isinstance(k, list) else str(k): canonical(v, with_ids=with_ids) for k, v in keys}
    if isinstance(o, (tuple, list)):
        return [canonical(x, with_ids=with_ids) for x in o]
    return o


def content_id(obj: object) -> str:
    """sha256 over the schema version and the canonical projection, first 16 hex chars."""
    blob = json.dumps([SCHEMA_VERSION, canonical(obj)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ statuses


class Origin(enum.StrEnum):
    """Who may assign a status. Adapters never assign one directly: the runner
    translates the adapter's response, and only from ADAPTER_REPORTABLE."""
    EXECUTION = "runner, from what the adapter process did"
    SCORING = "a ScoringDecision"
    DERIVED = "absence or conflict of evidence; never written"


class Status(enum.Enum):
    """PRD section 14 outcome classification for one (run, task, repetition).

    Credit rule (the ONLY status->number step, see Outcome.credit): SCORED
    earns its verdict score; every other status earns 0.0 and stays in the
    denominator. "Not applicable" is not a status: tasks outside
    package.eligible() never enter a plan, so applicability is decided by the
    package for every candidate at once.
    """
    SCORED = ("completed and scored", frozenset({Origin.SCORING}))
    INVALID_OUTPUT = ("invalid output", frozenset({Origin.EXECUTION, Origin.SCORING}))
    UNRESOLVED = ("unscored or disputed", frozenset({Origin.DERIVED}))
    UNSUPPORTED = ("unsupported capability", frozenset({Origin.EXECUTION}))
    BUILD_FAILURE = ("setup or build failure", frozenset({Origin.EXECUTION}))
    RUNTIME_FAILURE = ("runtime failure", frozenset({Origin.EXECUTION}))
    TIMEOUT = ("timeout", frozenset({Origin.EXECUTION}))
    BUDGET_EXHAUSTED = ("spending limit reached", frozenset({Origin.EXECUTION}))
    INTERRUPTED = ("started, never finished", frozenset({Origin.EXECUTION}))
    MISSING = ("missing result", frozenset({Origin.DERIVED}))

    def __init__(self, label: str, origins: frozenset[Origin]) -> None:
        self.label = label
        self.origins = origins


ADAPTER_REPORTABLE = frozenset({Status.UNSUPPORTED, Status.INVALID_OUTPUT, Status.RUNTIME_FAILURE})
"""Statuses an adapter response may carry besides success. TIMEOUT,
BUILD_FAILURE, BUDGET_EXHAUSTED and INTERRUPTED are observed by the runner."""

RETRYABLE = frozenset({Status.TIMEOUT, Status.INTERRUPTED})
"""Failures the runner observed (a timeout; a kill by signal or a harness crash mid-attempt). An
adapter-reported failure is final, so an adapter cannot discard an answer it dislikes and re-sample."""


def _require_origin(status: Status | None, origin: Origin, who: str) -> None:
    if status is not None and origin not in status.origins:
        raise ValueError(f"{who} may not assign {status.name}")


# -------------------------------------------------------------------- enums


class Mode(enum.StrEnum):
    CONTROLLED = "controlled"
    NATIVE = "native"


class Track(enum.StrEnum):
    CONSTRUCTION = "construction"
    SEMANTIC_TASK = "semantic_task"
    INTEROPERABILITY = "interoperability"
    ROBUSTNESS = "robustness"
    EVOLUTION = "evolution"
    OPERATIONAL = "operational"


class LadderLevel(enum.StrEnum):
    """PRD section 13. Baselines are ordinary configurations at the bottom rungs."""
    SCHEMA_ONLY = "schema_only"
    CONTEXT_RETRIEVAL = "context_retrieval"
    AUTO_ONTOLOGY = "auto_ontology"
    HUMAN_CORRECTED_ONTOLOGY = "human_corrected_ontology"
    EVOLVED_ONTOLOGY = "evolved_ontology"

    @property
    def is_baseline(self) -> bool:
        return self in (LadderLevel.SCHEMA_ONLY, LadderLevel.CONTEXT_RETRIEVAL)


class Purpose(enum.StrEnum):
    """Only SCORED runs enter cohorts. SMOKE runs use the CONFIG split.
    REPRODUCTION runs feed the reproduction section, never the ranking."""
    SMOKE = "smoke"
    SCORED = "scored"
    REPRODUCTION = "reproduction"


class Split(enum.StrEnum):
    CONFIG = "config"
    HELDOUT = "heldout"


class Disclosure(enum.StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"   # ids, statuses, scores exportable; content redacted


class ClaimStatus(enum.StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ScorerKind(enum.StrEnum):
    EXECUTABLE = "executable"
    HUMAN_REVIEW = "human_review"
    ADJUDICATION = "adjudication"


class InterventionKind(enum.StrEnum):
    HINT = "hint"
    MANUAL_FIX = "manual_fix"
    RESTART = "restart"
    CONFIG_CHANGE = "config_change"


class CorrectionKind(enum.StrEnum):
    VOID_RUN = "void_run"           # removes a run from cohorts; listed with its scores in every report
    CONFIRM_VOID = "confirm_void"   # a second person's sign-off on a VOID_RUN; publication waits for it
    ERRATUM = "erratum"


class VoidReason(enum.StrEnum):
    """Why a run may be voided. None of them is "the result was bad"."""
    HARNESS_BUG = "harness_bug"                    # a defect in the observatory itself
    INFRASTRUCTURE = "infrastructure"              # host or service outage outside the candidate
    PROTOCOL_VIOLATION = "protocol_violation"      # the evaluator broke the run protocol


# ------------------------------------------------------------------ registry


@dataclass(frozen=True)
class Candidate:
    """FR-01 registry entry. `claims` are attributed (tag -> ClaimStatus) and
    feed only the coverage matrix; they are never measured evidence and never
    change which tasks a candidate is planned on."""
    method_id: str
    name: str
    kind: str                                  # "platform" | "method" | "baseline"
    claims: tuple[tuple[str, ClaimStatus], ...]
    claim_source: str
    disclosures: tuple[str, ...] = ()          # funding, credits, vendor assistance


@dataclass(frozen=True)
class AdapterRef:
    command: tuple[str, ...]   # argv prefix; the runner appends the verb
    version: str
    digest: str = ""           # sha256 over the command's files (set by catalog.load), so an edited adapter is a new identity


@dataclass(frozen=True)
class ConfigurationIdentity:
    """PRD section 6 unit of evaluation, minus what the plan (package, mode,
    budget, reps) or manifest (environment, date) adds.

    information_access is disclosed, not keyed: the baseline ladder differs
    in information access by design and must still share a controlled cohort."""
    config_id: str
    method_id: str
    method_version: str
    model_id: str
    model_version: str
    ladder_level: LadderLevel
    settings: tuple[tuple[str, str], ...]
    information_access: frozenset[str]
    endpoint: str | None
    adapter: AdapterRef
    versions_immutable: bool                   # False => dated service observation

    @property
    def identity_hash(self) -> str:
        return content_id(self)


# ---------------------------------------------------------- benchmark package


@dataclass(frozen=True)
class PackageRef:
    """Exact identity. Same id@version with a different hash is a different
    package (and a catalog error: published versions are immutable)."""
    package_id: str
    version: str
    content_hash: str

    def __str__(self) -> str:
        return f"{self.package_id}@{self.version}"


@dataclass(frozen=True)
class ExecutableCheck:
    entrypoint: str   # "checks/sql_equal.py:check", relative to the package dir
    version: str      # sha256 of the check file; stamped on decisions


@dataclass(frozen=True)
class Rubric:
    text: str
    scale: tuple[float, ...]   # allowed values within [0, 1]


@dataclass(frozen=True)
class Task:
    """`check` is required, so a task without a reference check or rubric
    cannot exist (acceptance criterion 2)."""
    task_id: str
    group: str                    # dataset/scenario cluster for statistics
    tracks: frozenset[Track]
    tags: frozenset[str]          # coverage taxonomy "axis:category"
    split: Split
    disclosure: Disclosure
    difficulty: str
    consequence: str
    input_ref: str
    reference_ref: str | None     # None for rubric-only tasks
    check: ExecutableCheck | Rubric


@dataclass(frozen=True)
class Budget:
    """Owned by the package, per mode, so every cohort member ran under it."""
    max_usd: float
    build_timeout_s: float
    task_timeout_s: float
    memory_mb: int
    max_attempts: int

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass(frozen=True)
class AnalysisPlan:
    """Pre-registered: part of the package hash, fixed before any result.
    compare.* take no seed, margin or resample arguments."""
    min_meaningful_diff: float
    bootstrap_resamples: int
    seed: int
    min_groups: int          # fewer dataset/scenario groups => every call INCONCLUSIVE
    multiplicity: str        # "holm" | "none"; how pairwise calls are corrected


@dataclass(frozen=True)
class DataSnapshot:
    path: str
    sha256: str


@dataclass(frozen=True)
class BenchmarkPackage:
    ref: PackageRef
    suite: str                                 # frozen_core | fresh_challenge | ...
    provenance: str
    usage_rights: str
    taxonomy_version: str
    data: tuple[DataSnapshot, ...]
    tasks: tuple[Task, ...]
    budgets: tuple[tuple[Mode, Budget], ...]
    controlled_model: tuple[str, str]          # (model_id, model_version) imposed in CONTROLLED
    permitted_tools: tuple[str, ...]
    external_access: bool
    human_assistance: str
    analysis: AnalysisPlan
    limitations: tuple[str, ...] = ()

    def eligible(self, track: Track, split: Split) -> tuple[Task, ...]:
        """The ONLY definition of which tasks a plan contains. A property of the
        package, never of the candidate."""
        return tuple(t for t in self.tasks if track in t.tracks and t.split is split)

    def budget(self, mode: Mode) -> Budget:
        return dict(self.budgets)[mode]


@dataclass(frozen=True)
class ComparabilityKey:
    """Two runs may share a table iff their keys are equal. Derived from the
    plan, never authored. `model` is the imposed model in CONTROLLED mode and
    None in NATIVE mode, so the two modes can never produce equal keys."""
    package: PackageRef
    mode: Mode
    track: Track
    model: tuple[str, str] | None


# ------------------------------------------------------- ledger records (facts)


@dataclass(frozen=True)
class RunPlan:
    """Pre-registration, hashed before execution. Built only by runner.plan,
    which derives task_ids and budget from the package; Cohort re-checks the
    task list against the package so a hand-built plan cannot shrink it."""
    config: ConfigurationIdentity
    package: PackageRef
    mode: Mode
    track: Track
    purpose: Purpose
    repetitions: int
    task_ids: tuple[str, ...]
    budget: Budget

    def __post_init__(self) -> None:
        if self.repetitions < 1:
            raise ValueError("repetitions must be >= 1")

    @property
    def plan_id(self) -> str:
        return content_id(self)

    @property
    def split(self) -> Split:
        return Split.CONFIG if self.purpose is Purpose.SMOKE else Split.HELDOUT

    @property
    def key(self) -> ComparabilityKey:
        model = (self.config.model_id, self.config.model_version) if self.mode is Mode.CONTROLLED else None
        return ComparabilityKey(self.package, self.mode, self.track, model)


@dataclass(frozen=True)
class Incompatibility:
    """Returned by runner.plan instead of a plan (PRD section 7: report the
    incompatibility, never substitute a native result). Persisted by runner.plan
    itself and shown in the coverage matrix."""
    config_id: str
    package: PackageRef
    mode: Mode
    track: Track
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RunManifest:
    """Written once, before the first attempt. One run per plan: run_id is the plan_id, and the
    evaluator is metadata, so re-executing a plan resumes it and never samples it again."""
    plan: RunPlan
    started_at: datetime
    evaluator: str
    environment: tuple[tuple[str, str], ...]
    isolation: tuple[tuple[str, str], ...]     # what was actually enforced, e.g. network: not_enforced

    @property
    def run_id(self) -> str:
        return self.plan.plan_id

    @property
    def manifest_id(self) -> str:
        return content_id(self)


@dataclass(frozen=True)
class Cost:
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    measured: bool = True    # False => estimate; reports label it


@dataclass(frozen=True)
class Attempt:
    """One adapter invocation for (task, repetition). failure None == the
    adapter returned an output. The final attempt (max attempt_no) decides the
    execution side of the outcome."""
    run_id: str
    task_id: str
    repetition: int
    attempt_no: int
    failure: Status | None
    output_ref: str | None
    error: str
    wall_s: float
    cost: Cost
    finished_at: datetime
    model_used: tuple[str, str] | None = None   # as the adapter reported it; must equal the imposed model in CONTROLLED
    adapter_version: str | None = None          # as the adapter reported it

    def __post_init__(self) -> None:
        _require_origin(self.failure, Origin.EXECUTION, "the runner")

    @property
    def outcome_id(self) -> str:
        return f"{self.run_id}/{self.task_id}/{self.repetition}"


@dataclass(frozen=True)
class AttemptStart:
    """Written before each adapter invocation. A start with no Attempt after it is a harness crash
    mid-attempt; the runner resolves it to INTERRUPTED, which spends that attempt."""
    run_id: str
    task_id: str
    repetition: int
    attempt_no: int
    at: datetime


@dataclass(frozen=True)
class BuildRecord:
    run_id: str
    failure: Status | None       # None, BUILD_FAILURE or TIMEOUT
    error: str
    wall_s: float
    cost: Cost

    def __post_init__(self) -> None:
        _require_origin(self.failure, Origin.EXECUTION, "the runner")


@dataclass(frozen=True)
class Intervention:
    run_id: str
    at: datetime
    actor: str
    kind: InterventionKind
    minutes: float
    task_id: str | None
    note: str


@dataclass(frozen=True)
class Seal:
    """Written last. An unsealed run blocks publication."""
    run_id: str
    finished_at: datetime
    log_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Verdict:
    value: float
    critical_error: bool = False
    labels: tuple[str, ...] = ()   # failure categories for drill-down

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"score {self.value} outside [0, 1]")


@dataclass(frozen=True)
class ScoringDecision:
    """The only carrier of a score. Executable checks, human review and
    adjudication are the same record; a correction or adjudication is a new
    decision that `supersedes` earlier ones. Nothing is edited."""
    outcome_id: str
    manifest_id: str
    kind: ScorerKind
    scorer_id: str               # check version for EXECUTABLE, reviewer id otherwise
    status: Status               # SCORED or INVALID_OUTPUT
    verdict: Verdict | None
    rationale: str
    supersedes: tuple[str, ...]
    at: datetime = field(compare=False)

    def __post_init__(self) -> None:
        _require_origin(self.status, Origin.SCORING, "a scoring decision")
        if (self.status is Status.SCORED) != (self.verdict is not None):
            raise ValueError("a verdict is present exactly when status is SCORED")
        if self.kind is not ScorerKind.EXECUTABLE and not self.rationale.strip():
            raise ValueError("human scoring decisions require a rationale")
        if self.kind is ScorerKind.ADJUDICATION and not self.supersedes:
            raise ValueError("an adjudication must supersede the decisions it settles")

    @property
    def decision_id(self) -> str:
        return content_id([self.outcome_id, self.kind, self.scorer_id, self.status, self.verdict, list(self.supersedes)])


@dataclass(frozen=True)
class Correction:
    at: datetime
    author: str
    kind: CorrectionKind
    target: str                  # run_id or publication id
    reason: str
    reason_class: VoidReason | None = None   # required exactly on VOID_RUN

    def __post_init__(self) -> None:
        if (self.kind is CorrectionKind.VOID_RUN) != (self.reason_class is not None):
            raise ValueError("a void states its reason class; other corrections do not")


# ----------------------------------------------------------------- derived views


@dataclass(frozen=True)
class Outcome:
    """What one (run, task, repetition) counts as, carrying its own trace."""
    run_id: str
    task_id: str
    repetition: int
    status: Status
    manifest_id: str
    verdict: Verdict | None = None
    decision_id: str | None = None

    def __post_init__(self) -> None:
        if (self.status is Status.SCORED) != (self.verdict is not None):
            raise ValueError("a verdict is present exactly when status is SCORED")
        if self.status is Status.SCORED and not (self.decision_id and self.manifest_id):
            raise ValueError("a score without a manifest and scoring decision cannot exist")

    @property
    def credit(self) -> float:
        """The ONLY place a status becomes a number."""
        return self.verdict.value if self.verdict is not None else 0.0


def current_decisions(decisions: tuple[ScoringDecision, ...]) -> list[ScoringDecision]:
    """Decisions no other decision supersedes."""
    superseded = {s for d in decisions for s in d.supersedes}
    return [d for d in decisions if d.decision_id not in superseded]


def resolve_outcome(manifest: RunManifest, task_id: str, rep: int, build: BuildRecord | None,
                    final: Attempt | None,
                    decisions: tuple[ScoringDecision, ...]) -> Outcome:
    """The single mapping from facts to a status.

      build failed                       -> BUILD_FAILURE
      no attempt                         -> MISSING
      final attempt failed               -> that failure
      current decisions: none            -> UNRESOLVED
      current decisions disagree         -> UNRESOLVED (only adjudication settles)
      current decisions agree            -> their status/verdict (latest decision id)
    """
    rid, mid = manifest.run_id, manifest.manifest_id
    if build is not None and build.failure is not None:
        return Outcome(rid, task_id, rep, Status.BUILD_FAILURE, mid)
    if final is None:
        return Outcome(rid, task_id, rep, Status.MISSING, mid)
    if final.failure is not None:
        return Outcome(rid, task_id, rep, final.failure, mid)
    heads = current_decisions(decisions)
    if not heads or len({(d.status, d.verdict) for d in heads}) > 1:
        return Outcome(rid, task_id, rep, Status.UNRESOLVED, mid)
    d = max(heads, key=lambda d: d.at)
    return Outcome(rid, task_id, rep, d.status, mid, d.verdict, d.decision_id)


@dataclass(frozen=True)
class RunRecord:
    """A manifest plus everything the ledger holds about it, merged at read.

    `outcomes` is computed in the constructor and is total over
    plan.task_ids x range(plan.repetitions): no RunRecord can exist in which a
    planned (task, rep) lacks an outcome."""
    manifest: RunManifest
    build: BuildRecord | None
    attempts: tuple[Attempt, ...]
    interventions: tuple[Intervention, ...]
    seal: Seal | None
    decisions: tuple[ScoringDecision, ...]          # full history, superseded included
    void: Correction | None = None
    outcomes: Mapping[tuple[str, int], Outcome] = field(init=False, compare=False)
    final_attempts: Mapping[tuple[str, int], Attempt] = field(init=False, compare=False)   # the one per-slot rule

    def __post_init__(self) -> None:
        final: dict[tuple[str, int], Attempt] = {}
        for a in self.attempts:
            k = (a.task_id, a.repetition)
            if k not in final or a.attempt_no > final[k].attempt_no:
                final[k] = a
        object.__setattr__(self, "final_attempts", final)
        by_outcome: dict[str, list[ScoringDecision]] = {}
        for d in self.decisions:
            by_outcome.setdefault(d.outcome_id, []).append(d)
        plan, rid = self.manifest.plan, self.manifest.run_id
        out = {(t, r): resolve_outcome(self.manifest, t, r, self.build, final.get((t, r)),
                                       tuple(by_outcome.get(f"{rid}/{t}/{r}", ())))
               for t in plan.task_ids for r in range(plan.repetitions)}
        object.__setattr__(self, "outcomes", out)

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def assisted(self) -> bool:
        return bool(self.interventions)


@dataclass(frozen=True)
class Cohort:
    """The only input compare.summarize/paired accept.

    Checked on construction, whoever constructs it: package matches key; every
    run has this key, purpose SCORED, is not voided, and plans exactly
    package.eligible(key.track, HELDOUT); runs are filed under their own
    config, and all runs of one config share one identity_hash. `runs` holds ALL
    such runs of each member."""
    key: ComparabilityKey
    package: BenchmarkPackage
    runs: Mapping[str, tuple[RunRecord, ...]]       # config_id -> runs

    def __post_init__(self) -> None:
        if self.package.ref != self.key.package:
            raise Incomparable("package does not match the cohort's benchmark version")
        expected = tuple(t.task_id for t in self.package.eligible(self.key.track, Split.HELDOUT))
        for config_id, records in self.runs.items():
            if not records:
                raise Incomparable(f"{config_id} has no runs")
            if len(hashes := {r.manifest.plan.config.identity_hash for r in records}) > 1:
                raise Incomparable(f"{config_id} was edited between runs (identities {sorted(hashes)}); "
                                   "give the edited configuration a new config_id")
            for r in records:
                p = r.manifest.plan
                if p.key != self.key:
                    raise Incomparable(f"run {r.run_id} has key {p.key}, cohort has {self.key}")
                if p.purpose is not Purpose.SCORED:
                    raise Incomparable(f"run {r.run_id} is a {p.purpose} run")
                if p.config.config_id != config_id:
                    raise Incomparable(f"run {r.run_id} filed under the wrong configuration")
                if r.void is not None:
                    raise Incomparable(f"run {r.run_id} was voided: {r.void.reason}")
                if sorted(p.task_ids) != sorted(expected):
                    raise Incomparable(f"run {r.run_id} does not plan exactly the eligible tasks")
