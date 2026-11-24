"""Append-only evidence store on the filesystem (meant to live in a git repo).

    store/
      runs/<run_id>/
        manifest.json          # written once (O_EXCL); never rewritten
        build.json             # BuildRecord, written once
        attempts.jsonl         # runner is the only writer
        interventions.jsonl    # operator CLI is the only writer; refused after seal
        seal.json              # written once, last
        artifacts/<task>/<rep>/<attempt>/...
      decisions/<run_id>/<scorer_id>.jsonl   # one file per scorer: no shared writers
      incompatibilities.jsonl
      corrections.jsonl
      publications/<pub_id>/   # immutable snapshots, see report.publish

Ownership: each file has exactly one writer kind (runner, operator, a given
scorer, editor), so concurrent actors never write the same file
(separate-before-serializing-shared-state). Readers merge.

Serialization (dataclass <-> JSON) is private to this module; nothing else
sees a dict.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import types
from collections.abc import Iterator, Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

from .model import (
    Attempt, BuildRecord, Correction, CorrectionKind, Incompatibility, Intervention, RunManifest,
    RunRecord, ScoringDecision, Seal, Status,
)


class AppendOnlyViolation(RuntimeError):
    """Attempt to rewrite a write-once file, or append to a sealed run's logs."""


# ----------------------------------------------------- serialization (private)


def _key(k: object) -> str:
    j = to_jsonable(k)
    return "/".join(map(str, j)) if isinstance(j, list) else str(j)


def to_jsonable(o: object) -> object:
    """Dataclass/enum/datetime tree -> plain JSON values. Also used by report.export."""
    if is_dataclass(o) and not isinstance(o, type):
        return {f.name: to_jsonable(getattr(o, f.name)) for f in fields(o) if f.init}
    if isinstance(o, Status):
        return o.name
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(to_jsonable(x) for x in o)
    if isinstance(o, Mapping):
        return {_key(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (tuple, list)):
        return [to_jsonable(x) for x in o]
    return o


def _from(tp: object, v: object) -> object:
    if v is None:
        return None
    origin, args = get_origin(tp), get_args(tp)
    if origin in (Union, types.UnionType):
        return _from([a for a in args if a is not type(None)][0], v)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_from(args[0], x) for x in v)
        return tuple(_from(a, x) for a, x in zip(args, v))
    if origin is frozenset:
        return frozenset(_from(args[0], x) for x in v)
    if tp is Status:
        return Status[v]
    if tp is datetime:
        return datetime.fromisoformat(v)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return tp(v)
    if is_dataclass(tp):
        hints = get_type_hints(tp)
        return tp(**{f.name: _from(hints[f.name], v[f.name]) for f in fields(tp) if f.init and f.name in v})
    return v


def _dump(obj: object) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True) + "\n"


def _write_once(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(path, "x", encoding="utf-8")
    except FileExistsError:
        raise AppendOnlyViolation(f"{path} is write-once and already exists") from None
    with f:
        f.write(_dump(obj))
        f.flush()
        os.fsync(f.fileno())


def _append(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_dump(obj))
        f.flush()
        os.fsync(f.fileno())


def _read_one(path: Path, cls: type):
    return _from(cls, json.loads(path.read_text("utf-8"))) if path.exists() else None


def _read_lines(path: Path, cls: type) -> tuple:
    if not path.exists():
        return ()
    return tuple(_from(cls, json.loads(ln)) for ln in path.read_text("utf-8").splitlines() if ln.strip())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _rd(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def exists(self, run_id: str) -> bool:
        """Exact run id present (no prefix matching)."""
        return (self._rd(run_id) / "manifest.json").exists()

    def _manifest(self, run_id: str) -> RunManifest:
        if not self.exists(run_id):
            raise KeyError(f"no run {run_id}")
        return _read_one(self._rd(run_id) / "manifest.json", RunManifest)

    # -- writers (each called by exactly one actor kind) ----------------------

    def create_run(self, manifest: RunManifest) -> None:
        """Runner. Idempotent: same manifest_id already present -> no-op;
        different manifest under the same run_id -> AppendOnlyViolation."""
        if self.exists(manifest.run_id):
            if self._manifest(manifest.run_id).manifest_id == manifest.manifest_id:
                return
            raise AppendOnlyViolation(f"run {manifest.run_id} exists with a different manifest")
        _write_once(self._rd(manifest.run_id) / "manifest.json", manifest)

    def write_build(self, record: BuildRecord) -> None:
        """Runner. Write-once."""
        self._manifest(record.run_id)
        _write_once(self._rd(record.run_id) / "build.json", record)

    def _check_open(self, run_id: str) -> RunManifest:
        m = self._manifest(run_id)
        if (self._rd(run_id) / "seal.json").exists():
            raise AppendOnlyViolation(f"run {run_id} is sealed")
        return m

    def append_attempt(self, attempt: Attempt) -> None:
        """Runner. Slot-gated: raises AppendOnlyViolation if the run is sealed, the
        task is not in the plan, repetition >= plan.repetitions, or attempt_no is not
        exactly one more than the slot's last attempt and <= budget.max_attempts.
        So extra sampling needs a new pre-registered plan, never a quiet rerun.
        fsync after each line so a crash loses at most the in-flight attempt."""
        plan = self._check_open(attempt.run_id).plan
        if attempt.task_id not in plan.task_ids or not 0 <= attempt.repetition < plan.repetitions:
            raise AppendOnlyViolation(f"{attempt.outcome_id} is not a slot of the plan")
        path = self._rd(attempt.run_id) / "attempts.jsonl"
        last = max((a.attempt_no for a in _read_lines(path, Attempt)
                    if (a.task_id, a.repetition) == (attempt.task_id, attempt.repetition)), default=0)
        if attempt.attempt_no != last + 1 or attempt.attempt_no > plan.budget.max_attempts:
            raise AppendOnlyViolation(f"{attempt.outcome_id}: attempt {attempt.attempt_no} after {last} "
                                      f"(max {plan.budget.max_attempts})")
        _append(path, attempt)

    def append_intervention(self, iv: Intervention) -> None:
        """Operator. Raises AppendOnlyViolation if the run is sealed."""
        self._check_open(iv.run_id)
        _append(self._rd(iv.run_id) / "interventions.jsonl", iv)

    def seal(self, seal: Seal) -> None:
        """Runner. Write-once; computes and checks log hashes."""
        self._manifest(seal.run_id)
        rd = self._rd(seal.run_id)
        hashes = tuple((n, _sha(rd / n)) for n in ("attempts.jsonl", "interventions.jsonl"))
        if seal.log_hashes and seal.log_hashes != hashes:
            raise AppendOnlyViolation("seal log hashes do not match the logs")
        _write_once(rd / "seal.json", replace(seal, log_hashes=hashes))

    def append_decision(self, d: ScoringDecision) -> None:
        """Scorer. Writes to decisions/<run_id>/<scorer_id>.jsonl. Idempotent on decision_id."""
        run_id = d.outcome_id.split("/", 1)[0]
        self._manifest(run_id)
        path = self.root / "decisions" / run_id / f"{d.scorer_id}.jsonl"
        if any(x.decision_id == d.decision_id for x in _read_lines(path, ScoringDecision)):
            return
        _append(path, d)

    def append_incompatibility(self, inc: Incompatibility) -> None:
        path = self.root / "incompatibilities.jsonl"
        if inc not in _read_lines(path, Incompatibility):
            _append(path, inc)

    def append_correction(self, c: Correction) -> None:
        """Editor. Corrections are never removed; a mistaken one is countered by another."""
        _append(self.root / "corrections.jsonl", c)

    # -- readers -------------------------------------------------------------

    def run(self, run_id: str) -> RunRecord:
        """Merge every file of one run (manifest, build, attempts, interventions,
        seal, all scorer decision files, any VOID_RUN correction) into a RunRecord.
        Accepts any unambiguous run-id prefix."""
        if not self.exists(run_id):
            base = self.root / "runs"
            hits = sorted(p.name for p in base.glob(run_id + "*") if (p / "manifest.json").exists()) if base.exists() else []
            if len(hits) != 1:
                raise KeyError(f"run id {run_id!r} matches {len(hits)} runs")
            run_id = hits[0]
        rd = self._rd(run_id)
        decisions = tuple(d for f in sorted((self.root / "decisions" / run_id).glob("*.jsonl"))
                          for d in _read_lines(f, ScoringDecision))
        void = next((c for c in self.corrections() if c.kind is CorrectionKind.VOID_RUN and c.target == run_id), None)
        return RunRecord(self._manifest(run_id), _read_one(rd / "build.json", BuildRecord),
                         _read_lines(rd / "attempts.jsonl", Attempt), _read_lines(rd / "interventions.jsonl", Intervention),
                         _read_one(rd / "seal.json", Seal), decisions, void)

    def runs(self) -> Iterator[RunRecord]:
        base = self.root / "runs"
        for p in sorted(base.iterdir()) if base.exists() else ():
            if (p / "manifest.json").exists():
                yield self.run(p.name)

    def incompatibilities(self) -> tuple[Incompatibility, ...]:
        return _read_lines(self.root / "incompatibilities.jsonl", Incompatibility)

    def corrections(self) -> tuple[Correction, ...]:
        return _read_lines(self.root / "corrections.jsonl", Correction)

    def artifact_dir(self, run_id: str, task_id: str, rep: int, attempt_no: int) -> Path:
        return self.root / "runs" / run_id / "artifacts" / task_id / str(rep) / str(attempt_no)
