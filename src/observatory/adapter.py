"""Adapter contract (FR-03) and the one function that runs an adapter in isolation.

An adapter is an EXECUTABLE, not a Python class. Any language works, platform
developers can run it themselves to reproduce findings, and process isolation
plus resource limits come for free from the OS.

Protocol: the runner executes `<adapter.command...> <verb>` with:
  - cwd       = a fresh temporary dir, deleted after the call (nothing written there is kept)
  - stdin     = one JSON request object
  - stdout    = one JSON response object (anything else on stdout => INVALID_OUTPUT)
  - stderr    = free-form log, kept in the run's artifact dir
  - env       = scrubbed: PATH, HOME=<cwd>, OBS_STATE_DIR, plus secrets the
                config explicitly names. Nothing else is inherited.
stdin, stdout and stderr are anonymous temp files, and state_dir and build inputs are
staged copies in a scratch dir, so no request names a catalog or store path. The
filesystem is NOT sandboxed: an adapter running as the evaluator's user could still
search the disk (recorded as `filesystem: not_enforced`).

Verbs and requests (in CONTROLLED mode every request also carries "model": [model_id, model_version]):
  setup     {"settings": {...}, "state_dir": str}        # runs again on every resume: make it idempotent
  build     {"settings": {...}, "state_dir": str, "inputs": [paths], "information_access": [...]}
  run-task  {"settings": {...}, "state_dir": str, "task_id": str, "input": {...}}
  teardown  {"state_dir": str}
  (`update` is reserved for the P1 evolution track; MVP runner never calls it.)

Response (all verbs):
  {"status": "completed" | "unsupported" | "invalid_output" | "runtime_failure",
   "output": {...} | null,            # run-task only: the answer payload the check scores
   "cost": {"usd": float, "tokens_in": int, "tokens_out": int, "measured": bool},   # optional
   "adapter_version": str,
   "model_used": [model_id, model_version] | null}   # required in CONTROLLED mode: the model actually called
A cost, when present, must have exactly those four keys with a finite usd >= 0 and
integer token counts in [0, 2**53); anything else makes the response INVALID_OUTPUT.
An absent cost is recorded as unmeasured (measured=False, $0).

An adapter can report only model.ADAPTER_REPORTABLE; TIMEOUT, BUILD_FAILURE,
BUDGET_EXHAUSTED and INTERRUPTED are observed by the runner. An adapter cannot report "not applicable":
applicability belongs to the package.
"""

from __future__ import annotations

import enum
import json
import math
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .model import AdapterRef, Budget, Cost, Status


class Verb(enum.StrEnum):
    SETUP = "setup"
    BUILD = "build"
    RUN_TASK = "run-task"
    TEARDOWN = "teardown"


class AdapterNotStartable(RuntimeError):
    """The adapter command could not be spawned: a harness or catalog error, never a candidate's outcome."""


@dataclass(frozen=True)
class AdapterResult:
    """Parsed, validated response. Wire dicts never leave this module."""
    failure: Status | None       # None == completed; else ADAPTER_REPORTABLE, TIMEOUT or INTERRUPTED (killed)
    output: dict | None
    cost: Cost
    adapter_version: str | None
    wall_s: float
    stderr_path: Path
    error: str = ""
    model_used: tuple[str, str] | None = None


_WIRE = {"completed": None, "unsupported": Status.UNSUPPORTED, "invalid_output": Status.INVALID_OUTPUT,
         "runtime_failure": Status.RUNTIME_FAILURE}


def _limits(budget: Budget, timeout_s: float, report_fd: int | None = None):
    """preexec_fn applying rlimits; each is best effort (RLIMIT_AS is not settable on macOS).
    RLIMIT_FSIZE caps every file the child writes, its stdout and stderr included, at memory_mb.
    With report_fd, the child writes which limits it actually got."""
    def apply() -> None:
        got = []
        mem = budget.memory_mb * 1024 * 1024
        for name, res, val in (("rlimit_as", resource.RLIMIT_AS, mem), ("rlimit_cpu", resource.RLIMIT_CPU, math.ceil(timeout_s)),
                               ("rlimit_fsize", resource.RLIMIT_FSIZE, mem)):
            try:
                resource.setrlimit(res, (val, val))
                got.append(f"{name}=enforced")
            except (ValueError, OSError):
                got.append(f"{name}=not_enforced")
        if report_fd is not None:
            os.write(report_fd, ",".join(got).encode())
    return apply


def isolation(budget: Budget) -> tuple[tuple[str, str], ...]:
    """What invoke will actually enforce on this host, probed in a throwaway child.
    Recorded in the manifest's `isolation` field."""
    r, w = os.pipe()
    try:
        subprocess.run([sys.executable, "-c", "pass"], preexec_fn=_limits(budget, budget.task_timeout_s, w),
                       pass_fds=(w,), start_new_session=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        os.close(w)
        got = os.read(r, 4096).decode()
        os.close(r)
    probed = tuple(tuple(kv.split("=", 1)) for kv in got.split(",") if "=" in kv) or (("rlimits", "unknown"),)
    return ((("process_group", "enforced"), ("env", "scrubbed")) + tuple(probed)
            + (("filesystem", "not_enforced"), ("network", "not_enforced")))


def _cost(c: object) -> Cost:
    if c is None:
        return Cost(measured=False)
    if not isinstance(c, dict) or set(c) != {"usd", "tokens_in", "tokens_out", "measured"}:
        raise ValueError(f"cost must have exactly usd, tokens_in, tokens_out, measured: {c!r}")
    usd, tokens, measured = c["usd"], (c["tokens_in"], c["tokens_out"]), c["measured"]
    if type(usd) not in (int, float) or not math.isfinite(usd) or usd < 0:
        raise ValueError(f"cost.usd must be a finite number >= 0: {usd!r}")
    if any(type(t) is not int or not 0 <= t < 2 ** 53 for t in tokens):
        raise ValueError(f"token counts must be integers in [0, 2**53): {tokens!r}")
    if type(measured) is not bool:
        raise ValueError(f"cost.measured must be a boolean: {measured!r}")
    return Cost(float(usd), *tokens, measured)


def invoke(adapter: AdapterRef, verb: Verb, request: dict, *, artifact_dir: Path,
           state_dir: Path, timeout_s: float, budget: Budget,
           secrets: dict[str, str]) -> AdapterResult:
    """Run one adapter verb in a child process under limits. Never raises for
    adapter misbehaviour; every failure becomes a Status. Raises AdapterNotStartable
    when the command cannot be spawned at all.

    Isolation enforced (recorded into the manifest's `isolation` field by `isolation`):
      - own process group (start_new_session=True), killed whole when the call ends
      - RLIMIT_AS = budget.memory_mb, RLIMIT_CPU = timeout_s, RLIMIT_FSIZE = memory_mb (POSIX preexec_fn)
      - scrubbed env, a temporary cwd, stdio through anonymous files: no pipe a stray
        grandchild could hold open, so the call always returns at the timeout
      - filesystem and network: NOT enforced in MVP (see RATIONALE open questions)
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = artifact_dir / "stderr.log"
    cap = budget.memory_mb * 1024 * 1024
    t0 = time.monotonic()

    def done(failure: Status | None, error: str = "", output: dict | None = None, cost: Cost | None = None,
             version: str | None = None, model: tuple[str, str] | None = None) -> AdapterResult:
        return AdapterResult(failure, output, cost or Cost(measured=False), version, time.monotonic() - t0, stderr_path,
                             error, model)

    with (tempfile.TemporaryDirectory(prefix="obs-call-", ignore_cleanup_errors=True) as cwd, tempfile.TemporaryFile() as fin,
          tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr):
        fin.write(json.dumps(request).encode())
        fin.seek(0)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": cwd, "OBS_STATE_DIR": str(state_dir), **secrets}
        try:
            proc = subprocess.Popen([*adapter.command, verb.value], stdin=fin, stdout=fout, stderr=ferr, cwd=cwd,
                                    env=env, start_new_session=True, preexec_fn=_limits(budget, timeout_s))
        except (OSError, subprocess.SubprocessError) as e:
            raise AdapterNotStartable(f"cannot start adapter {list(adapter.command)}: {e}") from e
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill(proc)
            proc.wait()
        ferr.seek(0)
        stderr_path.write_bytes(ferr.read(cap))
        fout.seek(0)
        out = fout.read(cap)
    if timed_out:
        return done(Status.TIMEOUT, f"timed out after {timeout_s}s")
    if proc.returncode < 0:
        try:
            sig = signal.Signals(-proc.returncode).name
        except ValueError:
            sig = f"signal {-proc.returncode}"
        return done(Status.INTERRUPTED, f"resource_limit or crash: killed by {sig}")
    try:
        r = json.loads(out)
        failure = _WIRE[r["status"]]
        cost = _cost(r.get("cost"))
        output, version = r.get("output"), r.get("adapter_version")
        if failure is None and verb is Verb.RUN_TASK and not isinstance(output, dict):
            raise ValueError("completed run-task needs an output object")
        if version is not None and not isinstance(version, str):
            raise ValueError("adapter_version must be a string")
        model = r.get("model_used")
        if model is not None and not (isinstance(model, list) and len(model) == 2 and all(isinstance(x, str) for x in model)):
            raise ValueError("model_used must be [model_id, model_version]")
        model = model and tuple(model)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        if proc.returncode != 0:
            return done(Status.RUNTIME_FAILURE, f"exit {proc.returncode}, no valid response")
        return done(Status.INVALID_OUTPUT, f"malformed response: {e!r}")
    if proc.returncode != 0 and failure is None:
        return done(Status.RUNTIME_FAILURE, f"exit {proc.returncode} after reporting completed", cost=cost, version=version,
                    model=model)
    return done(failure, "" if failure is None else f"adapter reported {r['status']}",
                output if isinstance(output, dict) else None, cost, version, model)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
