"""Adapter contract (FR-03) and the one function that runs an adapter in isolation.

An adapter is an EXECUTABLE, not a Python class. Any language works, platform
developers can run it themselves to reproduce findings, and process isolation
plus resource limits come for free from the OS.

Protocol: the runner executes `<adapter.command...> <verb>` with:
  - cwd       = a fresh per-invocation workdir (artifacts written here are collected)
  - stdin     = one JSON request object
  - stdout    = one JSON response object (anything else on stdout => INVALID_OUTPUT)
  - stderr    = free-form log, captured to the artifact dir
  - env       = scrubbed: PATH, HOME=<workdir>, OBS_STATE_DIR, plus secrets the
                config explicitly names. Nothing else is inherited.

Verbs and requests:
  setup     {"settings": {...}, "state_dir": str}
  build     {"settings": {...}, "state_dir": str, "inputs": [paths], "information_access": [...]}
  run-task  {"settings": {...}, "state_dir": str, "task_id": str, "input": {...}}
  teardown  {"state_dir": str}
  (`update` is reserved for the P1 evolution track; MVP runner never calls it.)

Response (all verbs):
  {"status": "completed" | "unsupported" | "invalid_output" | "runtime_failure",
   "output": {...} | null,            # run-task only: the answer payload the check scores
   "cost": {"usd": float, "tokens_in": int, "tokens_out": int, "measured": bool},
   "adapter_version": str}

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
import time
from dataclasses import dataclass
from pathlib import Path

from .model import AdapterRef, Budget, Cost, Status


class Verb(enum.StrEnum):
    SETUP = "setup"
    BUILD = "build"
    RUN_TASK = "run-task"
    TEARDOWN = "teardown"


@dataclass(frozen=True)
class AdapterResult:
    """Parsed, validated response. Wire dicts never leave this module."""
    failure: Status | None       # None == completed; else ADAPTER_REPORTABLE or TIMEOUT
    output: dict | None
    cost: Cost
    adapter_version: str | None
    wall_s: float
    stderr_path: Path
    error: str = ""


_WIRE = {"completed": None, "unsupported": Status.UNSUPPORTED, "invalid_output": Status.INVALID_OUTPUT,
         "runtime_failure": Status.RUNTIME_FAILURE}


def _limits(budget: Budget, timeout_s: float, report_fd: int | None = None):
    """preexec_fn applying rlimits; each is best effort (RLIMIT_AS is not settable on macOS).
    With report_fd, the child writes which limits it actually got."""
    def apply() -> None:
        got = []
        for name, res, val in (("rlimit_as", resource.RLIMIT_AS, budget.memory_mb * 1024 * 1024),
                               ("rlimit_cpu", resource.RLIMIT_CPU, math.ceil(timeout_s))):
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
    return (("process_group", "enforced"), ("env", "scrubbed")) + tuple(probed) + (("network", "not_enforced"),)


def invoke(adapter: AdapterRef, verb: Verb, request: dict, *, workdir: Path,
           state_dir: Path, timeout_s: float, budget: Budget,
           secrets: dict[str, str]) -> AdapterResult:
    """Run one adapter verb in a child process under limits. Never raises for
    adapter misbehaviour; every failure becomes a Status.

    Isolation enforced (recorded into the manifest's `isolation` field by `isolation`):
      - own process group (start_new_session=True); on timeout kill the whole group
      - RLIMIT_AS = budget.memory_mb, RLIMIT_CPU = timeout_s (POSIX preexec_fn)
      - scrubbed env, per-invocation cwd
      - network: NOT enforced in MVP (see RATIONALE open questions)
    """
    workdir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = workdir / "stderr.log"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workdir), "OBS_STATE_DIR": str(state_dir), **secrets}
    t0 = time.monotonic()

    def done(failure: Status | None, error: str = "", output: dict | None = None, cost: Cost | None = None,
             version: str | None = None) -> AdapterResult:
        return AdapterResult(failure, output, cost or Cost(measured=False), version, time.monotonic() - t0, stderr_path, error)

    try:
        proc = subprocess.Popen([*adapter.command, verb.value], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=workdir, env=env, start_new_session=True,
                                preexec_fn=_limits(budget, timeout_s))
    except (OSError, subprocess.SubprocessError) as e:
        stderr_path.write_text(f"cannot start adapter: {e}\n")
        return done(Status.RUNTIME_FAILURE, f"cannot start adapter: {e}")
    timed_out = False
    try:
        out, err = proc.communicate(json.dumps(request).encode(), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        out, err = proc.communicate()
    finally:
        _kill(proc)  # stray grandchildren
    stderr_path.write_bytes(err)
    if timed_out:
        return done(Status.TIMEOUT, f"timed out after {timeout_s}s")
    if proc.returncode < 0:
        sig = signal.Signals(-proc.returncode).name
        return done(Status.RUNTIME_FAILURE, f"resource_limit or crash: killed by {sig}")
    try:
        r = json.loads(out)
        failure = _WIRE[r["status"]]
        c = r.get("cost") or {}
        cost = Cost(float(c.get("usd", 0.0)), int(c.get("tokens_in", 0)), int(c.get("tokens_out", 0)), bool(c.get("measured", True)))
        output, version = r.get("output"), r.get("adapter_version")
        if failure is None and verb is Verb.RUN_TASK and not isinstance(output, dict):
            raise ValueError("completed run-task needs an output object")
        if version is not None and not isinstance(version, str):
            raise ValueError("adapter_version must be a string")
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        if proc.returncode != 0:
            return done(Status.RUNTIME_FAILURE, f"exit {proc.returncode}, no valid response")
        return done(Status.INVALID_OUTPUT, f"malformed response: {e!r}")
    if proc.returncode != 0 and failure is None:
        return done(Status.RUNTIME_FAILURE, f"exit {proc.returncode} after reporting completed", cost=cost, version=version)
    return done(failure, "" if failure is None else f"adapter reported {r['status']}",
                output if isinstance(output, dict) else None, cost, version)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
