"""Append-only evidence store on the filesystem (meant to live in a git repo).

    store/
      runs/<run_id>/
        manifest.json          # written once (O_EXCL); never rewritten
        build.json             # BuildRecord, written once
        starts.jsonl           # AttemptStart before each adapter call; runner is the only writer
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

import contextlib
import enum
import fcntl
import hashlib
import json
import os
import types
import warnings
from collections.abc import Iterator
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

from .model import (
    ID_FIELDS, RETRYABLE, SCHEMA_VERSION, Attempt, AttemptStart, BuildRecord, Correction, CorrectionKind, Incompatibility, Intervention,
    RunManifest, RunRecord, ScoringDecision, Seal, Status, canonical, check_id,
)


class AppendOnlyViolation(RuntimeError):
    """Attempt to rewrite a write-once file, or append to a sealed run's logs."""


# ----------------------------------------------------- serialization (private)


def to_jsonable(o: object) -> object:
    """Dataclass/enum/datetime tree -> plain JSON values, content ids included. Also used by report.export."""
    return canonical(o, with_ids=True)


def _keys(tp: type) -> set[str]:
    names = {f.name for f in fields(tp) if f.init}
    return names | {ID_FIELDS[tp.__name__]} if tp.__name__ in ID_FIELDS else names


def _from(tp: object, v: object) -> object:
    """Exact inverse of to_jsonable: a missing or unknown key, an ambiguous union, or a stored content id
    that the content no longer hashes to is an error, never a silent default."""
    if v is None:
        return None
    origin, args = get_origin(tp), get_args(tp)
    if origin in (Union, types.UnionType):
        arms = [a for a in args if a is not type(None)]
        if len(arms) > 1:
            arms = [a for a in arms if is_dataclass(a) and isinstance(v, dict) and set(v) == _keys(a)]
            if len(arms) != 1:
                raise ValueError(f"stored value {v!r} matches {len(arms)} of {args}")
        return _from(arms[0], v)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_from(args[0], x) for x in v)
        return tuple(_from(a, x) for a, x in zip(args, v, strict=True))
    if origin is frozenset:
        return frozenset(_from(args[0], x) for x in v)
    if tp is Status:
        return Status[v]
    if tp is datetime:
        return datetime.fromisoformat(v)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return tp(v)
    if is_dataclass(tp):
        if not isinstance(v, dict) or set(v) != _keys(tp):
            raise ValueError(f"stored {tp.__name__} has keys {sorted(v)}, expected {sorted(_keys(tp))}")
        hints = get_type_hints(tp)
        obj = tp(**{f.name: _from(hints[f.name], v[f.name]) for f in fields(tp) if f.init})
        if (k := ID_FIELDS.get(tp.__name__)) and getattr(obj, k) != v[k]:
            raise ValueError(f"stored {tp.__name__} {k} {v[k]} does not match its content ({getattr(obj, k)})")
        return obj
    return v


def _dump(obj: object) -> str:
    return json.dumps({"schema_version": SCHEMA_VERSION, **to_jsonable(obj)}, sort_keys=True) + "\n"


def _parse(text: str, cls: type):
    v = json.loads(text)
    if not isinstance(v, dict) or v.pop("schema_version", None) != SCHEMA_VERSION:
        raise ValueError(f"stored {cls.__name__} is not schema_version {SCHEMA_VERSION}")
    return _from(cls, v)


def _write_once(path: Path, obj: object) -> None:
    """Complete or absent, never torn: write a temp file, then hard-link it into place (fails if present)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_dump(obj))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, path)
    except FileExistsError:
        raise AppendOnlyViolation(f"{path} is write-once and already exists") from None
    finally:
        tmp.unlink()


def _append(path: Path, obj: object) -> None:
    """Every line ends in a newline; an unterminated tail is a crashed write and is cut off first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        f.seek(0)
        data = f.read()
        if data and not data.endswith(b"\n"):
            f.truncate(data.rfind(b"\n") + 1)
        f.write(_dump(obj).encode())
        f.flush()
        os.fsync(f.fileno())


def _read_one(path: Path, cls: type):
    return _parse(path.read_text("utf-8"), cls) if path.exists() else None


def _read_lines(path: Path, cls: type) -> tuple:
    if not path.exists():
        return ()
    text = path.read_text("utf-8")
    lines = text.split("\n")
    if lines[-1]:
        warnings.warn(f"{path}: ignoring an incomplete final line (a write was interrupted)")
    return tuple(_parse(ln, cls) for ln in lines[:-1] if ln.strip())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()   # adapters run in other cwds; every path the store hands out is absolute

    def _rd(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    @contextlib.contextmanager
    def run_lock(self, run_id: str) -> Iterator[None]:
        """Runner. Exclusive while one process executes the run; a second executor gets RuntimeError."""
        with open(self._rd(run_id) / ".lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"run {run_id} is being executed by another process") from None
            yield

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

    def append_start(self, start: AttemptStart) -> None:
        """Runner. The slot gate: raises AppendOnlyViolation if the run is sealed, any start is still
        open, the task is not in the plan, repetition >= plan.repetitions, attempt_no is not exactly one
        more than the slot's last attempt or exceeds budget.max_attempts, or the last attempt is not
        RETRYABLE. So extra sampling needs a new pre-registered plan, never a quiet rerun."""
        plan = self._check_open(start.run_id).plan
        if start.task_id not in plan.task_ids or not 0 <= start.repetition < plan.repetitions:
            raise AppendOnlyViolation(f"{start.run_id}/{start.task_id}/{start.repetition} is not a slot of the plan")
        if self.open_starts(start.run_id):
            raise AppendOnlyViolation(f"run {start.run_id} has an unfinished attempt")
        slot = [a for a in _read_lines(self._rd(start.run_id) / "attempts.jsonl", Attempt)
                if (a.task_id, a.repetition) == (start.task_id, start.repetition)]
        last = max(slot, key=lambda a: a.attempt_no, default=None)
        n = last.attempt_no if last else 0
        if start.attempt_no != n + 1 or start.attempt_no > plan.budget.max_attempts or (last and last.failure not in RETRYABLE):
            raise AppendOnlyViolation(f"{start.run_id}/{start.task_id}/{start.repetition}: attempt {start.attempt_no} "
                                      f"after {n} ({last and last.failure}; max {plan.budget.max_attempts})")
        _append(self._rd(start.run_id) / "starts.jsonl", start)

    def open_starts(self, run_id: str) -> tuple[AttemptStart, ...]:
        """Starts with no attempt after them: attempts a crash left unfinished."""
        rd = self._rd(run_id)
        done = {(a.task_id, a.repetition, a.attempt_no) for a in _read_lines(rd / "attempts.jsonl", Attempt)}
        return tuple(s for s in _read_lines(rd / "starts.jsonl", AttemptStart)
                     if (s.task_id, s.repetition, s.attempt_no) not in done)

    def append_attempt(self, attempt: Attempt) -> None:
        """Runner. Finishes the open start of the same slot and attempt_no; anything else raises
        AppendOnlyViolation. fsync after each line so a crash loses at most the in-flight attempt."""
        self._check_open(attempt.run_id)
        if [(s.task_id, s.repetition, s.attempt_no) for s in self.open_starts(attempt.run_id)] != \
                [(attempt.task_id, attempt.repetition, attempt.attempt_no)]:
            raise AppendOnlyViolation(f"{attempt.outcome_id} attempt {attempt.attempt_no} was never started")
        _append(self._rd(attempt.run_id) / "attempts.jsonl", attempt)

    def append_intervention(self, iv: Intervention) -> None:
        """Operator. Raises AppendOnlyViolation if the run is sealed."""
        self._check_open(iv.run_id)
        _append(self._rd(iv.run_id) / "interventions.jsonl", iv)

    def seal(self, seal: Seal) -> None:
        """Runner. Write-once; computes and checks log hashes."""
        self._manifest(seal.run_id)
        if self.open_starts(seal.run_id):
            raise AppendOnlyViolation(f"run {seal.run_id} has an unfinished attempt")
        rd = self._rd(seal.run_id)
        hashes = tuple((n, _sha(rd / n)) for n in ("starts.jsonl", "attempts.jsonl", "interventions.jsonl"))
        if seal.log_hashes and seal.log_hashes != hashes:
            raise AppendOnlyViolation("seal log hashes do not match the logs")
        _write_once(rd / "seal.json", replace(seal, log_hashes=hashes))

    def append_decision(self, d: ScoringDecision) -> None:
        """Scorer. Writes to decisions/<run_id>/<scorer_id>.jsonl. Idempotent on decision_id."""
        run_id = check_id("run_id", d.outcome_id.split("/", 1)[0])
        check_id("scorer_id", d.scorer_id)
        if self._manifest(run_id).manifest_id != d.manifest_id:
            raise ValueError(f"decision for {d.outcome_id} names manifest {d.manifest_id}, not the run's")
        path = self.root / "decisions" / run_id / f"{d.scorer_id}.jsonl"
        if any(x.decision_id == d.decision_id for x in _read_lines(path, ScoringDecision)):
            return
        _append(path, d)

    def append_incompatibility(self, inc: Incompatibility) -> None:
        path = self.root / "incompatibilities.jsonl"
        if inc not in _read_lines(path, Incompatibility):
            _append(path, inc)

    def append_correction(self, c: Correction) -> None:
        """Editor. Corrections are never removed; a mistaken one is countered by another. A CONFIRM_VOID
        needs a VOID_RUN on the same target by someone else."""
        if c.kind is CorrectionKind.CONFIRM_VOID and not any(
                v.kind is CorrectionKind.VOID_RUN and v.target == c.target and v.author != c.author for v in self.corrections()):
            raise ValueError(f"no void of {c.target} by anyone other than {c.author} to confirm")
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
